#include "ydb_workload.h"
#include "ydb_workload_import.h"
#include "ydb_workload_tpcc.h"

#include "topic_workload/topic_workload.h"
#include "transfer_workload/transfer_workload.h"
#include "ydb_benchmark.h"

#include <ydb/library/yverify_stream/yverify_stream.h>

#include <ydb/library/workload/abstract/workload_factory.h>
#include <ydb/public/lib/ydb_cli/commands/ydb_common.h>
#include <ydb/public/lib/ydb_cli/common/recursive_remove.h>
#include <ydb/public/lib/yson_value/ydb_yson_value.h>
#include <ydb/public/sdk/cpp/include/ydb-cpp-sdk/client/topic/client.h>

#include <library/cpp/threading/local_executor/local_executor.h>

#include <util/system/spinlock.h>
#include <util/thread/pool.h>

#include <atomic>
#include <iomanip>

namespace NYdb::NConsoleClient {

struct TWorkloadStats {
    ui64 OpsCount;
    ui64 Percentile50;
    ui64 Percentile95;
    ui64 Percentile99;
    ui64 Percentile100;
};

TWorkloadStats GetWorkloadStats(const NHdr::THistogram& hdr) {
    TWorkloadStats stats;
    stats.OpsCount = hdr.GetTotalCount();
    stats.Percentile50 = hdr.GetValueAtPercentile(50.0);
    stats.Percentile95 = hdr.GetValueAtPercentile(95.0);
    stats.Percentile99 = hdr.GetValueAtPercentile(99.0);
    stats.Percentile100 = hdr.GetMax();
    return stats;
}

TCommandWorkload::TCommandWorkload()
    : TClientCommandTree("workload", {}, "YDB workload service")
{
    AddCommand(std::make_unique<TCommandWorkloadTopic>());
    AddCommand(std::make_unique<TCommandWorkloadTransfer>());
    AddCommand(std::make_unique<TCommandTPCC>());
    for (const auto& key: NYdbWorkload::TWorkloadFactory::GetRegisteredKeys()) {
        AddCommand(std::make_unique<TWorkloadCommandRoot>(key.c_str()));
    }
}

TWorkloadCommand::TWorkloadCommand(const TString& name, const std::initializer_list<TString>& aliases, const TString& description)
    : TYdbCommand(name, aliases, description)
    , TotalSec(0)
    , Threads(0)
    , Rate(0)
    , ClientTimeoutMs(0)
    , OperationTimeoutMs(0)
    , CancelAfterTimeoutMs(0)
    , WindowSec(0)
    , Quiet(false)
    , Verbose(false)
    , PrintTimestamp(false)
    , QueryExecuterType()
    , WindowHist(60000, 2) // highestTrackableValue 60000ms = 60s, precision 2
    , TotalHist(60000, 2)
    , TotalQueries(0)
    , TotalRetries(0)
    , WindowRetryCount(0)
    , TotalErrors(0)
    , WindowErrors(0)
{}

void TWorkloadCommand::Config(TConfig& config) {
    TYdbCommand::Config(config);

    config.Opts->AddLongOption('s', "seconds", "Seconds to run workload.")
        .DefaultValue(10).StoreResult(&TotalSec);
    config.Opts->AddLongOption('t', "threads", "Number of parallel threads in workload.")
        .DefaultValue(10).StoreResult(&Threads);

    const auto name = Parent->Parent->Name;
    if (name == "kv") {
        config.Opts->AddLongOption("rate", "Total rate for all threads (requests per second).")
            .DefaultValue(0).StoreResult(&Rate);
    }
    else if (name == "stock") {
        config.Opts->AddLongOption("rate", "Total rate for all threads (transactions per second).")
            .DefaultValue(0).StoreResult(&Rate);
    }

    config.Opts->AddLongOption("quiet", "Quiet mode. Doesn't print statistics each second.")
        .StoreTrue(&Quiet);
    config.Opts->AddLongOption("print-timestamp", "Print timestamp each second with statistics.")
        .StoreTrue(&PrintTimestamp);
    config.Opts->AddLongOption("client-timeout", "Client timeout in ms.")
        .DefaultValue(1000).StoreResult(&ClientTimeoutMs);
    config.Opts->AddLongOption("operation-timeout", "Operation timeout in ms.")
        .DefaultValue(800).StoreResult(&OperationTimeoutMs);
    config.Opts->AddLongOption("cancel-after", "Cancel after timeout in ms.")
        .DefaultValue(800).StoreResult(&CancelAfterTimeoutMs);
    config.Opts->AddLongOption("window", "Window duration in seconds.")
        .DefaultValue(1).StoreResult(&WindowSec);
    config.Opts->AddLongOption("executer", "Query executer type (data or generic).")
        .DefaultValue("generic").StoreResult(&QueryExecuterType);
}

void TWorkloadCommand::PrepareForRun(TConfig& config) {
    SetRandomSeed(Now().MicroSeconds());

    auto driverConfig = TDriverConfig()
        .SetEndpoint(config.Address)
        .SetDatabase(config.Database)
        .SetBalancingPolicy(EBalancingPolicy::UseAllNodes)
        .SetCredentialsProviderFactory(config.CredentialsGetter(config));

    Verbose = config.IsVerbose();
    if (config.EnableSsl) {
        driverConfig.UseSecureConnection(config.CaCerts);
    }
    Driver = std::make_unique<NYdb::TDriver>(NYdb::TDriver(driverConfig));
    auto tableClientSettings = NTable::TClientSettings()
                        .SessionPoolSettings(
                            NTable::TSessionPoolSettings()
                                .MaxActiveSessions(10+Threads));
    TableClient = std::make_unique<NTable::TTableClient>(*Driver, tableClientSettings);
    if (QueryExecuterType == "data") {
        // nothing to do
    } else if (QueryExecuterType == "generic") {
        auto queryClientSettings = NQuery::TClientSettings()
                            .SessionPoolSettings(
                                NQuery::TSessionPoolSettings()
                                    .MaxActiveSessions(10+Threads));
        QueryClient = std::make_unique<NQuery::TQueryClient>(*Driver, queryClientSettings);
    } else {
        throw TMisuseException() << "Unexpected executor Type: " << QueryExecuterType;
    }
}

void TWorkloadCommand::WorkerFn(int taskId, NYdbWorkload::IWorkloadQueryGenerator& workloadGen, const int type) {
    const auto dataQuerySettings = NYdb::NTable::TExecDataQuerySettings()
            .KeepInQueryCache(true)
            .OperationTimeout(TDuration::MilliSeconds(OperationTimeoutMs))
            .ClientTimeout(TDuration::MilliSeconds(ClientTimeoutMs))
            .CancelAfter(TDuration::MilliSeconds(CancelAfterTimeoutMs));
    const auto genericQuerySettings = NYdb::NQuery::TExecuteQuerySettings()
            .ClientTimeout(TDuration::MilliSeconds(ClientTimeoutMs));
    int retryCount = -1;
    NYdbWorkload::TQueryInfo queryInfo;

    auto runTableClient = [this, &queryInfo, &dataQuerySettings, &retryCount] (NYdb::NTable::TSession session) -> NYdb::TStatus {
        if (!TableClient) {
            Y_FAIL_S("TableClient is not initialized.");
        }
        ++retryCount;
        if (queryInfo.AlterTable) {
            auto result = TableClient->RetryOperationSync([&queryInfo](NTable::TSession session) {
                return session.AlterTable(queryInfo.TablePath, queryInfo.AlterTable.value()).GetValueSync();
            });
            return result;
        } else if (queryInfo.UseReadRows) {
            auto result = TableClient->ReadRows(queryInfo.TablePath, std::move(*queryInfo.KeyToRead))
                .GetValueSync();
            if (queryInfo.ReadRowsResultCallback) {
                queryInfo.ReadRowsResultCallback.value()(result);
            }
            return result;
        } else if (queryInfo.TableOperation) {
            auto result = queryInfo.TableOperation(*TableClient);
            return result;
        } else {
            auto mode = queryInfo.UseStaleRO ? NYdb::NTable::TTxSettings::StaleRO() : NYdb::NTable::TTxSettings::SerializableRW();
            auto result = session.ExecuteDataQuery(queryInfo.Query.c_str(),
                NYdb::NTable::TTxControl::BeginTx(mode).CommitTx(),
                queryInfo.Params, dataQuerySettings
            ).GetValueSync();
            if (queryInfo.DataQueryResultCallback) {
                queryInfo.DataQueryResultCallback.value()(result);
            }
            return result;
        }
    };

    auto runQueryClient = [this, &queryInfo, &genericQuerySettings, &retryCount] (NYdb::NQuery::TSession session) -> NYdb::NQuery::TAsyncExecuteQueryResult {
        if (!QueryClient) {
            Y_FAIL_S("QueryClient is not initialized.");
        }
        ++retryCount;
        if (queryInfo.AlterTable) {
            throw TMisuseException() << "Generic query doesn't support alter table. Use data query (--executer data)";
        } else if (queryInfo.UseReadRows) {
            throw TMisuseException() << "Generic query doesn't support readrows. Use data query (--executer data)";
        } else {
            auto result = session.ExecuteQuery(queryInfo.Query.c_str(),
                NYdb::NQuery::TTxControl::BeginTx(NYdb::NQuery::TTxSettings::SerializableRW()).CommitTx(),
                queryInfo.Params, genericQuerySettings
            );
            return result;
        }
    };

    auto runQuery = [this, &runQueryClient, &runTableClient, &queryInfo]() -> NYdb::TStatus {
        if (QueryExecuterType == "data") {
            return TableClient->RetryOperationSync(runTableClient);
        } else {
            auto result = QueryClient->RetryQuery(runQueryClient).GetValueSync();
            if (queryInfo.GenericQueryResultCallback) {
                queryInfo.GenericQueryResultCallback.value()(result);
            }
            return result;
        }
    };

    while (Now() < StopTime) {
        auto queryInfoList = workloadGen.GetWorkload(type);
        if (queryInfoList.empty()) {
            Cerr << "Task ID: " << taskId << ". No queries to run." << Endl;
            return;
        }
        std::vector<NYdbWorkload::TQueryInfo> shuffledQueries(queryInfoList.cbegin(), queryInfoList.cend());
        std::random_shuffle(shuffledQueries.begin(), shuffledQueries.end());

        for (const auto& q: shuffledQueries) {
            queryInfo = q;
            auto opStartTime = Now();
            if (opStartTime >= StopTime) {
                break;
            }
            if (Rate != 0)
            {
                const ui64 expectedQueries = (Now() - StartTime).SecondsFloat() * Rate;
                if (TotalQueries > expectedQueries) {
                    Sleep(TDuration::MilliSeconds(1));
                    continue;
                }
            }

            auto status = queryInfo.TableOperation ? TableClient->RetryOperationSync(runTableClient) : runQuery();
            if (status.IsSuccess()) {
                ui64 latency = (Now() - opStartTime).MilliSeconds();
                with_lock(HdrLock) {
                    WindowHist.RecordValue(latency);
                    TotalHist.RecordValue(latency);
                }
                TotalQueries++;
            } else {
                TotalErrors++;
                WindowErrors++;
                if (Verbose && status.GetStatus() != EStatus::ABORTED) {
                    Cerr << "Task ID: " << taskId << " Status: " << status.GetStatus() << " " << status.GetIssues().ToString() << Endl
                    << " Query text: " << queryInfo.Query << Endl;
                }
            }
            if (retryCount > 0) {
                TotalRetries += retryCount;
                WindowRetryCount += retryCount;
            }
            retryCount = -1;
        }
    }
    TotalRetries += std::max(retryCount, 0);
    WindowRetryCount += std::max(retryCount, 0);
}

int TWorkloadCommand::RunWorkload(NYdbWorkload::IWorkloadQueryGenerator& workloadGen, const int type) {
    if (!Quiet) {
        std::cout << "Window\t" << std::setw(7) << "Txs" << "\tTxs/Sec\tRetries\tErrors\tp50(ms)\tp95(ms)\tp99(ms)\tpMax(ms)";
        if (PrintTimestamp) {
            std::cout << "\tTimestamp";
        }
        std::cout << std::endl;
    }

    StartTime = Now();
    StopTime = StartTime + TDuration::Seconds(TotalSec);

    NPar::LocalExecutor().RunAdditionalThreads(Threads);

    auto futures = NPar::LocalExecutor().ExecRangeWithFutures([this, &workloadGen, type](int id) {
        try {
            WorkerFn(id, workloadGen, type);
        } catch (std::exception& error) {
            Y_FAIL_S(error.what());
        }
    }, 0, Threads, NPar::TLocalExecutor::MED_PRIORITY);

    int windowIt = 1;
    auto windowDuration = TDuration::Seconds(WindowSec);
    while (Now() < StopTime) {
        if (StartTime + windowIt * windowDuration < Now()) {
            PrintWindowStats(windowIt++);
        }
        Sleep(std::max(TDuration::Zero(), Now() - StartTime - windowIt * windowDuration));
    }

    for (auto f : futures) {
        f.Wait();
    }

    PrintWindowStats(windowIt++);

    auto stats = GetWorkloadStats(TotalHist);
    std::cout << std::endl << "Total\t" << std::setw(7) << "Txs" << "\tTxs/Sec\tRetries\tErrors\tp50(ms)\tp95(ms)\tp99(ms)\tpMax(ms)" << std::endl
        << windowIt - 1 << "\t"
        << std::setw(7) << stats.OpsCount << "\t" << stats.OpsCount / (TotalSec * 1.0) << "\t" << TotalRetries.load() << "\t"
        << TotalErrors.load() << "\t" << stats.Percentile50 << "\t" << stats.Percentile95 << "\t"
        << stats.Percentile99 << "\t" << stats.Percentile100 << std::endl;

    return EXIT_SUCCESS;
}

void TWorkloadCommand::PrintWindowStats(int windowIt) {
    TWorkloadStats stats;
    auto retries = WindowRetryCount.exchange(0);
    auto errors = WindowErrors.exchange(0);
    with_lock(HdrLock) {
        stats = GetWorkloadStats(WindowHist);
        WindowHist.Reset();
    }
    if (!Quiet) {
        std::cout << windowIt << "\t" << std::setw(7) << stats.OpsCount << "\t" << stats.OpsCount / WindowSec << "\t" << retries << "\t"
            << errors << "\t" << stats.Percentile50 << "\t" << stats.Percentile95 << "\t"
            << stats.Percentile99 << "\t" << stats.Percentile100;
        if (PrintTimestamp) {
            std::cout << "\t" << Now().ToStringUpToSeconds();
        }
        std::cout << std::endl;
    }
}

TWorkloadCommandInit::TWorkloadCommandInit(NYdbWorkload::TWorkloadParams& params)
    : TWorkloadCommandBase("init", params, NYdbWorkload::TWorkloadParams::ECommandType::Init, "Create and initialize tables for workload")
{}

void TWorkloadCommandInit::Config(TConfig& config) {
    TWorkloadCommandBase::Config(config);
    config.Opts->AddLongOption("clear", "Clear tables before init")
        .Optional().StoreTrue(&Clear);
}

TWorkloadCommandRun::TWorkloadCommandRun(NYdbWorkload::TWorkloadParams& params, const NYdbWorkload::IWorkloadQueryGenerator::TWorkloadType& workload)
    : TWorkloadCommand(workload.CommandName, std::initializer_list<TString>(), workload.Description)
    , Params(params)
    , Type(workload.Type)
{
}

int TWorkloadCommandRun::Run(TConfig& config) {
    PrepareForRun(config);
    Params.SetClients(QueryClient.get(), nullptr, TableClient.get(), nullptr);
    Params.DbPath = config.Database;
    Params.Verbose = config.IsVerbose();
    auto workloadGen = Params.CreateGenerator();
    Params.Validate(NYdbWorkload::TWorkloadParams::ECommandType::Run, Type);
    Params.Init();
    workloadGen->Init();
    return RunWorkload(*workloadGen, Type);
}

void TWorkloadCommandRun::Config(TConfig& config) {
    TWorkloadCommand::Config(config);
    config.Opts->SetFreeArgsNum(0);
    Params.ConfigureOpts(config.Opts->GetOpts(), NYdbWorkload::TWorkloadParams::ECommandType::Run, Type);
}

TWorkloadCommandBase::TWorkloadCommandBase(const TString& name, NYdbWorkload::TWorkloadParams& params, const NYdbWorkload::TWorkloadParams::ECommandType commandType, const TString& description, int type)
    : TYdbCommand(name, std::initializer_list<TString>(), description)
    , CommandType(commandType)
    , Params(params)
    , Type(type)
{
    if (const auto desc = Params.GetDescription(CommandType, Type)) {
        Description = desc;
    }
}

void TWorkloadCommandBase::Config(TConfig& config) {
    TYdbCommand::Config(config);
    config.Opts->SetFreeArgsNum(0);
    config.Opts->AddLongOption("dry-run", "Dry run")
        .Optional().StoreTrue(&DryRun);
    Params.ConfigureOpts(config.Opts->GetOpts(), CommandType, Type);
}

int TWorkloadCommandBase::Run(TConfig& config) {
    if (!DryRun) {
        Driver = MakeHolder<NYdb::TDriver>(CreateDriver(config));
        TableClient = MakeHolder<NTable::TTableClient>(*Driver);
        TopicClient = MakeHolder<NTopic::TTopicClient>(*Driver);
        SchemeClient = MakeHolder<NScheme::TSchemeClient>(*Driver);
        QueryClient = MakeHolder<NQuery::TQueryClient>(*Driver);
        Params.SetClients(QueryClient.Get(), SchemeClient.Get(), TableClient.Get(), TopicClient.Get());
    }
    Params.DbPath = config.Database;
    Params.Verbose = config.IsVerbose();
    auto workloadGen = Params.CreateGenerator();
    auto result = DoRun(*workloadGen, config);
    if (!DryRun) {
        Params.SetClients(nullptr, nullptr, nullptr, nullptr);
        TableClient->Stop().Wait();
        QueryClient.Reset();
        SchemeClient.Reset();
        TopicClient.Reset();
        TableClient.Reset();
        Driver->Stop(true);
        Driver.Reset();
    }
    return result;
}

void TWorkloadCommandBase::CleanTables(NYdbWorkload::IWorkloadQueryGenerator& workloadGen, TConfig& config) {
    auto pathsToDelete = workloadGen.GetCleanPaths();
    TRemoveDirectoryRecursiveSettings settings;
    settings.NotExistsIsOk(true);
    settings.CreateProgressBar(true);
    for (const auto& path : pathsToDelete) {
        Cout << "Remove path " << path << "..."  << Endl;
        auto fullPath = config.Database + "/" + path.c_str();
        if (DryRun) {
            Cout << "Remove " << fullPath << Endl;
        } else {
            NStatusHelpers::ThrowOnErrorOrPrintIssues(RemovePathRecursive(*Driver.Get(), fullPath, settings));
            RmParentIfEmpty(path, config);
        }
        Cout << "Remove path " << path << "...Ok"  << Endl;
    }
}

void TWorkloadCommandBase::RmParentIfEmpty(TStringBuf path, TConfig& config) {
    path.RNextTok('/');
    if (!path) {
        return;
    }
    auto fullPath = std::string(config.Database.c_str()) + "/" + std::string(path.cbegin(), path.cend());
    auto lsResult = SchemeClient->ListDirectory(fullPath).GetValueSync();
    if (lsResult.IsSuccess() && lsResult.GetChildren().empty() && lsResult.GetEntry().Type == NScheme::ESchemeEntryType::Directory) {
        Cout << "Folder " << path << " is empty, remove it..." << Endl;
        NStatusHelpers::ThrowOnErrorOrPrintIssues(SchemeClient->RemoveDirectory(fullPath).GetValueSync());
    }
    RmParentIfEmpty(path, config);
}

std::unique_ptr<TClientCommand> TWorkloadCommandRoot::CreateRunCommand(const NYdbWorkload::IWorkloadQueryGenerator::TWorkloadType& workload) {
    switch (workload.Kind) {
    case NYdbWorkload::IWorkloadQueryGenerator::TWorkloadType::EKind::Workload:
        return std::make_unique<TWorkloadCommandRun>(*Params, workload);
    case NYdbWorkload::IWorkloadQueryGenerator::TWorkloadType::EKind::Benchmark:
        return std::make_unique<TWorkloadCommandBenchmark>(*Params, workload);
    }
}

TWorkloadCommandRoot::TWorkloadCommandRoot(const TString& key)
    : TClientCommandTree(key, {}
        , "YDB " + NYdbWorkload::TWorkloadFactory::MakeHolder(key)->GetWorkloadName() + " workload"
      )
    , Params(NYdbWorkload::TWorkloadFactory::MakeHolder(key))
{
    if (const auto desc = Params->GetDescription(NYdbWorkload::TWorkloadParams::ECommandType::Root, 0)) {
        Description = desc;
    }
    AddCommand(std::make_unique<TWorkloadCommandInit>(*Params));
    if (auto import = TWorkloadCommandImport::Create(*Params)) {
        AddCommand(std::move(import));
    }
    auto supportedWorkloads = Params->CreateGenerator()->GetSupportedWorkloadTypes();
    switch (supportedWorkloads.size()) {
    case 0:
        break;
    case 1:
        supportedWorkloads.back().CommandName = "run";
        AddCommand(CreateRunCommand(supportedWorkloads.back()));
        break;
    default: {
        auto run = std::make_unique<TClientCommandTree>("run", std::initializer_list<TString>(), "Run YDB " + NYdbWorkload::TWorkloadFactory::MakeHolder(key)->GetWorkloadName() + " workload");
        for (const auto& type: supportedWorkloads) {
            run->AddCommand(CreateRunCommand(type));
        }
        AddCommand(std::move(run));
        break;
    }
    }
    AddCommand(std::make_unique<TWorkloadCommandClean>(*Params));
}

void TWorkloadCommandRoot::Config(TConfig& config) {
    TClientCommandTree::Config(config);
    Params->ConfigureOpts(config.Opts->GetOpts(), NYdbWorkload::TWorkloadParams::ECommandType::Root, 0);
}

int TWorkloadCommandInit::DoRun(NYdbWorkload::IWorkloadQueryGenerator& workloadGen, TConfig& config) {
    if (Clear) {
        CleanTables(workloadGen, config);
    }
    auto ddlQueries = workloadGen.GetDDLQueries();
    if (!ddlQueries.empty()) {
        Cout << "Init tables ..."  << Endl;
        if (DryRun) {
            Cout << ddlQueries << Endl;
        } else {
            TVector<TString> existPaths;
            for (const auto& path: workloadGen.GetCleanPaths()) {
                const auto fullPath = config.Database + "/" + path.c_str();
                if (SchemeClient->DescribePath(fullPath).GetValueSync().IsSuccess()) {
                    existPaths.emplace_back(path);
                }
            }
            if (existPaths) {
                throw yexception() << "Paths " << JoinSeq(", ", existPaths) << " already exist. Use 'ydb wokload " << Params.GetWorkloadName() << " clean' command or '--clear' option of 'init' command to cleanup tables.";
            }
            auto result = TableClient->RetryOperationSync([ddlQueries](NTable::TSession session) {
                return session.ExecuteSchemeQuery(ddlQueries.c_str()).GetValueSync();
            });
            NStatusHelpers::ThrowOnErrorOrPrintIssues(result);
        }
        Cout << "Init tables ...Ok"  << Endl;
    }

    auto queryInfoList = workloadGen.GetInitialData();
    if (DryRun) {
        for (auto queryInfo : queryInfoList) {
            Cout << queryInfo.Query << Endl;
            if (!queryInfo.Params.Empty()) {
                Cout << "With: " << Endl;
                const auto pValues = queryInfo.Params.GetValues();
                for (const auto& [pName, pValue]: pValues) {
                    Cout << "    " << pName << " = " << FormatValueYson(pValue) << Endl;
                }
            }
            Cout << Endl;
        }
    } else {
        for (auto queryInfo : queryInfoList) {
            auto result = QueryClient->ExecuteQuery(
                queryInfo.Query.c_str(),
                NYdb::NQuery::TTxControl::BeginTx(NYdb::NQuery::TTxSettings::SerializableRW()).CommitTx(),
                std::move(queryInfo.Params)).GetValueSync();
            if (!result.IsSuccess()) {
                Cerr << "Query execution failed: " << result.GetIssues().ToString() << Endl
                    << "Query:\n" << queryInfo.Query << Endl;
                return EXIT_FAILURE;
            }
        }
    }
    return EXIT_SUCCESS;
}

TWorkloadCommandClean::TWorkloadCommandClean(NYdbWorkload::TWorkloadParams& params)
    : TWorkloadCommandBase("clean", params, NYdbWorkload::TWorkloadParams::ECommandType::Clean, "Drop tables created in init phase")
{}

int TWorkloadCommandClean::DoRun(NYdbWorkload::IWorkloadQueryGenerator& workloadGen, TConfig& config) {
    CleanTables(workloadGen, config);
    return EXIT_SUCCESS;
}

} // namespace NYdb::NConsoleClient
