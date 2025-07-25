#include "dq_opt_join.h"
#include "dq_opt_phy.h"

#include <yql/essentials/core/yql_join.h>
#include <yql/essentials/core/yql_opt_utils.h>
#include <ydb/library/yql/dq/type_ann/dq_type_ann.h>
#include <yql/essentials/utils/log/log.h>
#include <yql/essentials/providers/common/provider/yql_provider.h>
#include <yql/essentials/core/yql_type_helpers.h>

namespace NYql::NDq {

using namespace NYql::NNodes;

namespace {

struct TJoinInputDesc {
    TJoinInputDesc(TMaybe<THashSet<TStringBuf>> labels, const TExprBase& input,
        TSet<std::pair<TStringBuf, TStringBuf>>&& keys)
        : Labels(labels)
        , Input(input)
        , Keys(std::move(keys)) {}

    bool IsRealTable() const {
        return Labels.Defined();
    }

    TMaybe<THashSet<TStringBuf>> Labels; // defined for real table input only, empty otherwise
    TExprBase Input;
    TSet<std::pair<TStringBuf, TStringBuf>> Keys; // set of (label, column_name) pairs in this input
};

void CollectJoinColumns(const TExprBase& joinSettings, THashMap<TStringBuf, TVector<TStringBuf>>* columnsToRename,
    THashSet<TStringBuf>* columnsToDrop)
{
    for (const auto& option : joinSettings.Ref().Children()) {
        if (option->Head().IsAtom("rename")) {
            TCoAtom fromName{option->Child(1)};
            YQL_ENSURE(!fromName.Value().empty());
            TCoAtom toName{option->Child(2)};
            if (!toName.Value().empty()) {
                (*columnsToRename)[fromName.Value()].emplace_back(toName.Value());
            } else {
                columnsToDrop->emplace(fromName.Value());
            }
        }
    }
}

TExprBase BuildSkipNullKeys(TExprContext& ctx, TPositionHandle pos,
    const TExprBase& input, const TVector<TCoAtom>& keys)
{
    return Build<TCoSkipNullMembers>(ctx, pos)
        .Input(input)
        .Members()
            .Add(keys)
            .Build()
        .Done();
};

TExprBase BuildDqJoinInput(TExprContext& ctx, TPositionHandle pos, const TExprBase& input, const TVector<TCoAtom>& keys, bool any) {
    if (!any) {
        return input;
    }

    auto keyExtractor = ctx.Builder(pos)
        .Lambda()
            .Param("item")
            .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                auto listBuilder = parent.List();
                int pos = 0;
                for (const auto& key : keys) {
                    listBuilder
                        .Callable(pos++, "Member")
                            .Arg(0, "item")
                            .Add(1, key.Ptr())
                        .Seal();
                }

                return listBuilder.Seal();
            })
        .Seal()
        .Build();

    auto condense = Build<TCoLambda>(ctx, pos)
        .Args({"list"})
        .Body<TCoCondense1>()
            .Input("list")
            .InitHandler(BuildIdentityLambda(pos, ctx))
            .SwitchHandler()
                .Args({"item", "state"})
                .Body<TCoAggrNotEqual>()
                    .Left<TExprApplier>().Apply(TCoLambda(keyExtractor)).With(0, "item")
                        .Build()
                    .Right<TExprApplier>().Apply(TCoLambda(keyExtractor)).With(0, "state")
                        .Build()
                    .Build()
                .Build()
            .UpdateHandler()
                .Args({"item", "state"})
                .Body("state")
            .Build()
        .Build()
        .Done();

    auto partition = Build<TCoPartitionsByKeys>(ctx, pos)
        .Input(input)
        .KeySelectorLambda(keyExtractor)
        .SortDirections<TCoVoid>()
            .Build()
        .SortKeySelectorLambda<TCoVoid>()
            .Build()
        .ListHandlerLambda(condense)
        .Done();

    return partition;
}

TExprNode::TPtr CreateLabelList(const THashSet<TStringBuf>& labels, const TPositionHandle& position, TExprContext& ctx) {
    TExprNode::TListType newKeys;
    for (const auto& label : labels) {
        newKeys.push_back(ctx.NewAtom(position, label));
    }
    return ctx.NewList(position, std::move(newKeys));
}

TMaybe<TJoinInputDesc> BuildDqJoin(
    const TCoEquiJoinTuple& joinTuple,
    const THashMap<TStringBuf, TJoinInputDesc>& inputs,
    EHashJoinMode mode,
    TExprContext& ctx,
    const TTypeAnnotationContext& typeCtx,
    TVector<TString>& subtreeLabels,
    const NYql::TOptimizerHints& hints,
    bool useCBO
)
{
    TMaybe<TJoinInputDesc> left;
    TVector<TString> lhsLabels;
    TStringBuf leftLabel;
    TStringBuf rightLabel;
    if (joinTuple.LeftScope().Maybe<TCoAtom>()) {
        lhsLabels.push_back(joinTuple.LeftScope().Cast<TCoAtom>().StringValue());
        left = inputs.at(joinTuple.LeftScope().Cast<TCoAtom>().Value());
        leftLabel = joinTuple.LeftScope().Cast<TCoAtom>().Value();
        YQL_ENSURE(left, "unknown scope " << joinTuple.LeftScope().Cast<TCoAtom>().Value());
    } else {
        left = BuildDqJoin(joinTuple.LeftScope().Cast<TCoEquiJoinTuple>(), inputs, mode, ctx, typeCtx, lhsLabels, hints, useCBO);
        if (!left) {
            return {};
        }
    }

    TMaybe<TJoinInputDesc> right;
    TVector<TString> rhsLabels;
    if (joinTuple.RightScope().Maybe<TCoAtom>()) {
        rhsLabels.push_back(joinTuple.RightScope().Cast<TCoAtom>().StringValue());
        right = inputs.at(joinTuple.RightScope().Cast<TCoAtom>().Value());
        rightLabel = joinTuple.RightScope().Cast<TCoAtom>().Value();
        YQL_ENSURE(right, "unknown scope " << joinTuple.RightScope().Cast<TCoAtom>().Value());
    } else {
        right = BuildDqJoin(joinTuple.RightScope().Cast<TCoEquiJoinTuple>(), inputs, mode, ctx, typeCtx, rhsLabels, hints, useCBO);
        if (!right) {
            return {};
        }
    }

    subtreeLabels.insert(subtreeLabels.end(), std::make_move_iterator(lhsLabels.begin()), std::make_move_iterator(lhsLabels.end()));
    subtreeLabels.insert(subtreeLabels.end(), std::make_move_iterator(rhsLabels.begin()), std::make_move_iterator(rhsLabels.end()));

    auto options = joinTuple.Options();
    auto linkSettings = GetEquiJoinLinkSettings(options.Ref());
    for (auto& hint: hints.JoinAlgoHints->Hints) {
        if (
            std::unordered_set<std::string>(hint.JoinLabels.begin(), hint.JoinLabels.end()) ==
            std::unordered_set<std::string>(subtreeLabels.begin(), subtreeLabels.end())
        ) {
            linkSettings.JoinAlgo = hint.Algo;
            hint.Applied = true;
        }
    }
    YQL_ENSURE(linkSettings.JoinAlgo != EJoinAlgoType::StreamLookupJoin || typeCtx.StreamLookupJoin, "Unsupported join strategy: streamlookup");

    if (linkSettings.JoinAlgo == EJoinAlgoType::MapJoin) {
        mode = EHashJoinMode::Map;
    } else if (linkSettings.JoinAlgo == EJoinAlgoType::GraceJoin) {
        mode = EHashJoinMode::GraceAndSelf;
    }

    bool leftAny = linkSettings.LeftHints.contains("any");
    bool rightAny = linkSettings.RightHints.contains("any");

    TStringBuf joinType = joinTuple.Type().Value();
    TSet<std::pair<TStringBuf, TStringBuf>> resultKeys;
    if (joinType != TStringBuf("RightOnly") && joinType != TStringBuf("RightSemi")) {
        resultKeys.insert(left->Keys.begin(), left->Keys.end());
    }
    if (joinType != TStringBuf("LeftOnly") && joinType != TStringBuf("LeftSemi")) {
        resultKeys.insert(right->Keys.begin(), right->Keys.end());
    }

    auto leftTableLabel = left->IsRealTable() ? (left->Labels->size() > 1 ? CreateLabelList(*(left->Labels), left->Input.Pos(), ctx)
                                                                          : BuildAtom(leftLabel, left->Input.Pos(), ctx).Ptr())
                                              : Build<TCoVoid>(ctx, left->Input.Pos()).Done().Ptr();

    auto rightTableLabel = right->IsRealTable() ? (right->Labels->size() > 1 ? CreateLabelList(*(right->Labels), right->Input.Pos(), ctx)
                                                                             : BuildAtom(rightLabel, right->Input.Pos(), ctx).Ptr())
                                                : Build<TCoVoid>(ctx, right->Input.Pos()).Done().Ptr();

    size_t joinKeysCount = joinTuple.LeftKeys().Size() / 2;
    TVector<TCoAtom> leftJoinKeys;
    leftJoinKeys.reserve(joinKeysCount);
    TVector<TCoAtom> rightJoinKeys;
    rightJoinKeys.reserve(joinKeysCount);
    TVector<TCoAtom> leftJoinKeyNames;
    leftJoinKeyNames.reserve(joinKeysCount);
    TVector<TCoAtom> rightJoinKeyNames;
    rightJoinKeyNames.reserve(joinKeysCount);
    auto joinAlgo = BuildAtom(ToString(linkSettings.JoinAlgo), joinTuple.Pos(), ctx).Ptr();

    auto joinKeysBuilder = Build<TDqJoinKeyTupleList>(ctx, left->Input.Pos());

    for (size_t i = 0; i < joinKeysCount; ++i) {
        size_t keyIndex = i * 2;

        auto leftScopeAtom = joinTuple.LeftKeys().Item(keyIndex);
        auto leftColumnAtom = joinTuple.LeftKeys().Item(keyIndex + 1);
        auto rightScopeAtom = joinTuple.RightKeys().Item(keyIndex);
        auto rightColumnAtom = joinTuple.RightKeys().Item(keyIndex + 1);

        auto leftKey = Build<TCoAtom>(ctx, left->Input.Pos())
            .Value(left->IsRealTable()
                ? ToString(leftColumnAtom.Value())
                : FullColumnName(leftScopeAtom.Value(), leftColumnAtom.Value()))
            .Done();
        auto rightKey = Build<TCoAtom>(ctx, right->Input.Pos())
            .Value(right->IsRealTable()
                ? ToString(rightColumnAtom.Value())
                : FullColumnName(rightScopeAtom.Value(), rightColumnAtom.Value()))
            .Done();

        auto leftColumnName = Build<TCoAtom>(ctx, left->Input.Pos()).Value(FullColumnName(leftScopeAtom.Value(), leftColumnAtom.Value())).Done();
        auto rightColumnName = Build<TCoAtom>(ctx, right->Input.Pos()).Value(FullColumnName(rightScopeAtom.Value(), rightColumnAtom.Value())).Done();

        joinKeysBuilder.Add<TDqJoinKeyTuple>()
            .LeftLabel(leftScopeAtom)
            .LeftColumn(leftColumnAtom)
            .RightLabel(rightScopeAtom)
            .RightColumn(rightColumnAtom)
            .Build();

        leftJoinKeys.emplace_back(leftKey);
        rightJoinKeys.emplace_back(rightKey);

        leftJoinKeyNames.emplace_back(leftColumnName);
        rightJoinKeyNames.emplace_back(rightColumnName);
    }

    bool needAnyJoinFallback = linkSettings.JoinAlgo != EJoinAlgoType::StreamLookupJoin && (EHashJoinMode::Off == mode || EHashJoinMode::Map == mode);

    auto dqJoinBuilder =
        Build<TDqJoin>(ctx, joinTuple.Pos())
            .LeftInput(BuildDqJoinInput(ctx, joinTuple.Pos(), left->Input, leftJoinKeys, needAnyJoinFallback && leftAny))
            .LeftLabel(leftTableLabel)
            .RightInput(BuildDqJoinInput(ctx, joinTuple.Pos(), right->Input, rightJoinKeys, needAnyJoinFallback && rightAny))
            .RightLabel(rightTableLabel)
            .JoinType(joinTuple.Type())
            .JoinKeys(joinKeysBuilder.Done())
            .LeftJoinKeyNames()
                .Add(leftJoinKeyNames)
                .Build()
            .RightJoinKeyNames()
                .Add(rightJoinKeyNames)
                .Build()
            .JoinAlgo(joinAlgo);

    auto getShuffleByExprListFromSettings = [&](const TVector<NDq::TJoinColumn>& shuffleBy) -> TExprNode::TListType {
        TExprNode::TListType shuffleByExprList;

        for (const auto& column: shuffleBy) {
            auto node =
                ctx.Builder(joinTuple.Pos())
                    .List()
                        .Atom(0, column.RelName)
                        .Atom(1, column.AttributeName)
                    .Seal()
                .Build();

            shuffleByExprList.emplace_back(std::move(node));
        }

        return shuffleByExprList;
    };

    auto getShuffleByExprListFromJoinKeys = [&](const TVector<TCoAtom>& joinKeys) {
        TExprNode::TListType shuffleByExprList;

        for (const auto& column: joinKeys) {
            auto node =
                ctx.Builder(joinTuple.Pos())
                    .List()
                        .Atom(0, column.StringValue())
                    .Seal()
                .Build();

            shuffleByExprList.emplace_back(std::move(node));
        }

        return shuffleByExprList;
    };

    TExprNode::TListType shuffleLhsBy;
    if (useCBO) {
        shuffleLhsBy = getShuffleByExprListFromSettings(linkSettings.ShuffleLhsBy);
    } else {
        shuffleLhsBy = getShuffleByExprListFromJoinKeys(leftJoinKeys);
    }
    dqJoinBuilder
        .ShuffleLeftSideBy()
            .Add(std::move(shuffleLhsBy))
            .Build();

    TExprNode::TListType shuffleRhsBy;
    if (useCBO) {
        shuffleRhsBy = getShuffleByExprListFromSettings(linkSettings.ShuffleRhsBy);
    } else {
        shuffleRhsBy = getShuffleByExprListFromJoinKeys(rightJoinKeys);
    }
    dqJoinBuilder
        .ShuffleRightSideBy()
            .Add(std::move(shuffleRhsBy))
            .Build();

    if ((linkSettings.JoinAlgo != EJoinAlgoType::StreamLookupJoin && (EHashJoinMode::Off == mode || EHashJoinMode::Map == mode)) || !(leftAny || rightAny || !linkSettings.JoinAlgoOptions.empty())) {
        auto dqJoin = dqJoinBuilder.Done();
        return TJoinInputDesc(Nothing(), dqJoin, std::move(resultKeys));
    } else {
        TVector<TCoAtom> flags;
        if (leftAny) {
            flags.emplace_back(ctx.NewAtom(joinTuple.Pos(), "LeftAny", TNodeFlags::Default));
        }
        if (rightAny) {
            flags.emplace_back(ctx.NewAtom(joinTuple.Pos(), "RightAny", TNodeFlags::Default));
        }

        TVector<TCoNameValueTuple> joinAlgoOptions;
        for (ui32 i = 0; i + 1 < linkSettings.JoinAlgoOptions.size(); i += 2) {
            joinAlgoOptions.push_back(
                    Build<TCoNameValueTuple>(ctx, joinTuple.Pos())
                        .Name().Build(linkSettings.JoinAlgoOptions[i])
                        .Value<TCoAtom>().Build(linkSettings.JoinAlgoOptions[i + 1])
                        .Done());
        }

        auto dqJoin =
            dqJoinBuilder
                .JoinAlgoOptions()
                    .Add(std::move(joinAlgoOptions))
                .Build()
                .Flags<TCoAtomList>()
                    .Add(flags)
                .Build();

        return TJoinInputDesc(Nothing(), dqJoin.Done(), std::move(resultKeys));
    }
}

TMaybe<TJoinInputDesc> PrepareJoinInput(const TCoEquiJoinInput& input) {
    THashSet<TStringBuf> labels;
    if (input.Scope().Maybe<TCoAtom>()) {
        labels.insert(input.Scope().Cast<TCoAtom>().Value());
    } else {
        auto list = input.Scope().Cast<TCoAtomList>();
        for (auto atomLabel : list) {
            labels.insert(atomLabel.Value());
        }
    }

    auto listType = input.List().Ref().GetTypeAnn()->Cast<TListExprType>();
    auto resultStructType = listType->GetItemType()->Cast<TStructExprType>();

    TSet<std::pair<TStringBuf, TStringBuf>> keys;
    for (auto member : resultStructType->GetItems()) {
        if (input.Scope().Maybe<TCoAtom>()) {
            keys.emplace(input.Scope().Cast<TCoAtom>().Value(), member->GetName());
        } else {
            auto fullMemberName = member->GetName();
            if (fullMemberName.find(".") != TString::npos) {
                TStringBuf table;
                TStringBuf column;
                SplitTableName(fullMemberName, table, column);
                keys.emplace(table, column);
            } else {
                return {};
            }
        }
    }

    return TJoinInputDesc(labels, input.List(), std::move(keys));
}

TStringBuf RotateRightJoinType(TStringBuf joinType) {
    if (joinType == "Right") {
        return "Left";
    }
    if (joinType == "RightOnly") {
        return "LeftOnly";
    }
    if (joinType == "RightSemi") {
        return "LeftSemi";
    }
    YQL_ENSURE(false, "unexpected right join type " << joinType);
}

std::pair<TVector<TCoAtom>, TVector<TCoAtom>> GetJoinKeys(const TDqJoin& join, TExprContext& ctx) {
    TVector<TCoAtom> leftJoinKeys;
    TVector<TCoAtom> rightJoinKeys;

    auto size = join.JoinKeys().Size();
    leftJoinKeys.reserve(size);
    rightJoinKeys.reserve(size);

    for (const auto& keyTuple : join.JoinKeys()) {
        auto leftLabel = keyTuple.LeftLabel().Value();
        auto rightLabel = keyTuple.RightLabel().Value();

        auto leftKey = Build<TCoAtom>(ctx, join.Pos())
            .Value((join.LeftLabel().Maybe<TCoAtom>() || keyTuple.LeftColumn().Value().starts_with("_yql_dq_key_left_")) && !join.LeftLabel().Maybe<TCoAtomList>()
                ? keyTuple.LeftColumn().StringValue()
                : FullColumnName(leftLabel, keyTuple.LeftColumn().Value()))
            .Done();

        auto rightKey = Build<TCoAtom>(ctx, join.Pos())
            .Value((join.RightLabel().Maybe<TCoAtom>() || keyTuple.RightColumn().Value().starts_with("_yql_dq_key_right_")) && !join.RightLabel().Maybe<TCoAtomList>()
                ? keyTuple.RightColumn().StringValue()
                : FullColumnName(rightLabel, keyTuple.RightColumn().Value()))
            .Done();

        leftJoinKeys.emplace_back(std::move(leftKey));
        rightJoinKeys.emplace_back(std::move(rightKey));
    }

    return std::make_pair(std::move(leftJoinKeys), std::move(rightJoinKeys));
}

TDqJoinBase DqMakePhyMapJoin(const TDqJoin& join, const TExprBase& leftInput, const TExprBase& rightInput,
    TExprContext& ctx, bool useGraceCore)
{
    static const std::set<std::string_view> supportedTypes = {"Inner"sv, "Left"sv, "LeftOnly"sv, "LeftSemi"sv};
    auto joinType = join.JoinType().Value();
    bool supportedJoin = supportedTypes.contains(joinType);
    YQL_ENSURE(supportedJoin, "" << joinType);

    auto [leftJoinKeys, rightJoinKeys] = GetJoinKeys(join, ctx);

    TVector<TCoAtom> leftFilterKeys;
    TVector<TCoAtom> rightFilterKeys;

    if (joinType == "Inner"sv || joinType == "LeftSemi"sv) {
        for (const auto& key : leftJoinKeys) {
            leftFilterKeys.push_back(key);
        }
    }

    for (const auto& key : rightJoinKeys) {
        rightFilterKeys.push_back(key);
    }

    auto leftFilteredInput = BuildSkipNullKeys(ctx, join.Pos(), leftInput, leftFilterKeys);
    auto rightFilteredInput = BuildSkipNullKeys(ctx, join.Pos(), rightInput, rightFilterKeys);

    if (useGraceCore) {
        auto flags = Build<TCoAtomList>(ctx, join.Pos())
            .Add<TCoAtom>().Value("Broadcast").Build()
            .Done();

        return Build<TDqPhyGraceJoin>(ctx, join.Pos())
            .LeftInput(leftFilteredInput)
            .LeftLabel(join.LeftLabel())
            .RightInput(rightFilteredInput)
            .RightLabel(join.RightLabel())
            .JoinType(join.JoinType())
            .JoinKeys(join.JoinKeys())
            .LeftJoinKeyNames(join.LeftJoinKeyNames())
            .RightJoinKeyNames(join.RightJoinKeyNames())
            .Flags(flags)
            .Done();
    } else {
        return Build<TDqPhyMapJoin>(ctx, join.Pos())
            .LeftInput(leftFilteredInput)
            .LeftLabel(join.LeftLabel())
            .RightInput(rightFilteredInput)
            .RightLabel(join.RightLabel())
            .JoinType(join.JoinType())
            .JoinKeys(join.JoinKeys())
            .LeftJoinKeyNames(join.LeftJoinKeyNames())
            .RightJoinKeyNames(join.RightJoinKeyNames())
            .Done();
    }
}

} // namespace

// used in yql_dq_recapture.cpp
bool CheckJoinColumns(const TExprBase& node) {
    try {
        auto equiJoin = node.Cast<TCoEquiJoin>();
        THashMap<TStringBuf, TVector<TStringBuf>> columnsToRename;
        THashSet<TStringBuf> columnsToDrop;
        CollectJoinColumns(equiJoin.Arg(equiJoin.ArgCount() - 1), &columnsToRename, &columnsToDrop);
        return true;
    } catch (...) {
        return false;
    }
}

TExprBase DqRewriteEquiJoin(
    const TExprBase& node,
    EHashJoinMode mode,
    bool useCBO,
    TExprContext& ctx,
    TTypeAnnotationContext& typeCtx,
    const TOptimizerHints& hints
) {
    int dummyJoinCounter = 0;
    return DqRewriteEquiJoin(node, mode, useCBO, ctx, typeCtx, dummyJoinCounter, hints);
}

/**
 * Rewrite `EquiJoin` to a number of `DqJoin` callables. This is done to simplify next step of building
 * physical stages with join operators.
 * Potentially this optimizer can also perform joins reorder given cardinality information.
 */
TExprBase DqRewriteEquiJoin(
    const TExprBase& node,
    EHashJoinMode mode,
    bool useCBO,
    TExprContext& ctx,
    TTypeAnnotationContext& typeCtx,
    int& joinCounter,
    const TOptimizerHints& hints
) {
    if (!node.Maybe<TCoEquiJoin>()) {
        return node;
    }
    auto equiJoin = node.Cast<TCoEquiJoin>();
    YQL_ENSURE(equiJoin.ArgCount() >= 4);

    THashMap<TStringBuf, TJoinInputDesc> inputs;
    for (size_t i = 0; i < equiJoin.ArgCount() - 2; ++i) {
        if (auto input = PrepareJoinInput(equiJoin.Arg(i).Cast<TCoEquiJoinInput>())) {
            for (auto label : *(input->Labels)) {
                inputs.emplace(label, *input);
            }
        } else {
            return node;
        }
    }

    auto joinTuple = equiJoin.Arg(equiJoin.ArgCount() - 2).Cast<TCoEquiJoinTuple>();
    TVector<TString> dummy;
    auto result = BuildDqJoin(joinTuple, inputs, mode, ctx, typeCtx, dummy, hints, useCBO);
    if (!result) {
        return node;
    }

    auto equiJoinStats = typeCtx.GetStats(equiJoin.Raw());
    typeCtx.SetStats(result->Input.Raw(), equiJoinStats);

    THashMap<TStringBuf, TVector<TStringBuf>> columnsToRename;
    THashSet<TStringBuf> columnsToDrop;
    CollectJoinColumns(equiJoin.Arg(equiJoin.ArgCount() - 1), &columnsToRename, &columnsToDrop);

    if (columnsToRename.empty() && columnsToDrop.empty()) {
        return result->Input;
    }

    joinCounter += equiJoin.ArgCount() - 2;

    auto row = Build<TCoArgument>(ctx, node.Pos())
            .Name("row")
            .Done();

    TVector<TExprBase> members;
    for (auto key : result->Keys) {
        auto fqColumnName = FullColumnName(key.first, key.second);
        if (columnsToDrop.contains(fqColumnName)) {
            continue;
        }

        auto member = Build<TCoMember>(ctx, node.Pos())
            .Struct(row)
            .Name().Build(fqColumnName)
            .Done();

        auto* renames = columnsToRename.FindPtr(fqColumnName);
        if (renames) {
            for (const auto& name : *renames) {
                members.emplace_back(
                    Build<TCoNameValueTuple>(ctx, node.Pos())
                        .Name().Build(name)
                        .Value(member)
                        .Done());
            }
        } else {
            members.emplace_back(
                Build<TCoNameValueTuple>(ctx, node.Pos())
                    .Name().Build(fqColumnName)
                    .Value(member)
                    .Done());
        }
    }

    auto projection = Build<TCoMap>(ctx, node.Pos())
        .Input(result->Input)
        .Lambda()
            .Args({row})
            .Body<TCoAsStruct>()
                .Add(members)
                .Build()
            .Build()
        .Done();

    return projection;
}

TDqJoin DqSuppressSortOnJoinInput(const TDqJoin& join, TExprContext& ctx) {
    const bool lOrdered = join.LeftInput().Ref().GetConstraint<TSortedConstraintNode>() || join.LeftInput().Ref().GetConstraint<TChoppedConstraintNode>();
    const bool rOrdered = join.RightInput().Ref().GetConstraint<TSortedConstraintNode>() || join.RightInput().Ref().GetConstraint<TChoppedConstraintNode>();

    if (lOrdered && rOrdered)
        return Build<TDqJoin>(ctx, join.Pos())
            .InitFrom(join)
            .LeftInput<TCoUnordered>()
                .Input(join.LeftInput())
                .Build()
            .RightInput<TCoUnordered>()
                .Input(join.RightInput())
                .Build()
            .Done();
    else if (lOrdered)
        return Build<TDqJoin>(ctx, join.Pos())
            .InitFrom(join)
            .LeftInput<TCoUnordered>()
                .Input(join.LeftInput())
                .Build()
            .Done();
    else if (rOrdered)
        return Build<TDqJoin>(ctx, join.Pos())
            .InitFrom(join)
            .RightInput<TCoUnordered>()
                .Input(join.RightInput())
                .Build()
            .Done();
    return join;
}

TExprBase DqRewriteRightJoinToLeft(const TExprBase node, TExprContext& ctx) {
    if (!node.Maybe<TDqJoin>()) {
        return node;
    }

    auto dqJoin = node.Cast<TDqJoin>();
    if (!dqJoin.JoinType().Value().StartsWith("Right")) {
        return node;
    }

    TMaybeNode<TCoAtomList> newFlags;
    if (TMaybeNode<TCoAtomList> flags = dqJoin.Flags()) {
        auto flagsBuilder = Build<TCoAtomList>(ctx, flags.Cast().Pos());
        for (auto flag: flags.Cast()) {
            TStringBuf tail;
            if( flag.Value().AfterPrefix("Left", tail)) {
                flagsBuilder.Add().Value("Right" + TString(tail)).Build();
            } else if ( flag.Value().AfterPrefix("Right", tail)) {
                flagsBuilder.Add().Value("Left" + TString(tail)).Build();
            } else {
                flagsBuilder.Add(flag);
            }
        }
        newFlags = flagsBuilder.Done();
    }

    auto joinKeysBuilder = Build<TDqJoinKeyTupleList>(ctx, dqJoin.Pos());
    for (const auto& keys : dqJoin.JoinKeys()) {
        joinKeysBuilder.Add<TDqJoinKeyTuple>()
            .LeftLabel(keys.RightLabel())
            .LeftColumn(keys.RightColumn())
            .RightLabel(keys.LeftLabel())
            .RightColumn(keys.LeftColumn())
            .Build();
    }

    return Build<TDqJoin>(ctx, dqJoin.Pos())
        .LeftInput(dqJoin.RightInput())
        .RightInput(dqJoin.LeftInput())
        .LeftLabel(dqJoin.RightLabel())
        .RightLabel(dqJoin.LeftLabel())
        .JoinType()
            .Value(RotateRightJoinType(dqJoin.JoinType().Value()))
            .Build()
        .JoinKeys(joinKeysBuilder.Done())
        .LeftJoinKeyNames(dqJoin.LeftJoinKeyNames())
        .RightJoinKeyNames(dqJoin.RightJoinKeyNames())
        .JoinAlgo(dqJoin.JoinAlgo())
        .ShuffleLeftSideBy(dqJoin.ShuffleLeftSideBy())
        .ShuffleRightSideBy(dqJoin.ShuffleRightSideBy())
        .JoinAlgoOptions(dqJoin.JoinAlgoOptions())
        .Flags(newFlags)
        .Done();
}

TExprBase DqRewriteLeftPureJoin(const TExprBase node, TExprContext& ctx, const TParentsMap& parentsMap,
    bool allowStageMultiUsage)
{
    if (!node.Maybe<TDqJoin>()) {
        return node;
    }

    auto join = node.Cast<TDqJoin>();

    static const std::set<std::string_view> supportedTypes = {"Left"sv, "LeftOnly"sv, "LeftSemi"sv};
    auto joinType = join.JoinType().Value();
    if (!supportedTypes.contains(joinType)) {
        return node;
    }

    if (!join.RightInput().Maybe<TDqCnUnionAll>()) {
        return node;
    }

    if (!IsDqCompletePureExpr(join.LeftInput())) {
        return node;
    }

    auto rightConnection = join.RightInput().Cast<TDqCnUnionAll>();

    if (!IsSingleConsumerConnection(rightConnection, parentsMap, allowStageMultiUsage)) {
        return node;
    }

    auto leftStage = Build<TDqStage>(ctx, join.Pos())
        .Inputs()
            .Build()
        .Program()
            .Args({})
            .Body<TCoToFlow>()
                .Input(join.LeftInput())
                .Build()
            .Build()
        .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
        .Done();

    auto leftConnection = Build<TDqCnUnionAll>(ctx, join.Pos())
        .Output()
            .Stage(leftStage)
            .Index().Build("0")
            .Build()
        .Done();

    // TODO: Right input might be large, there are better possible physical plans.
    // We only need matching key from the right side. Instead of broadcasting
    // all right input data to single task, we can do a "partial" right semi join
    // on in the right stage to extract only necessary rows.
    return Build<TDqJoin>(ctx, join.Pos())
        .InitFrom(join)
        .LeftInput(leftConnection)
        .JoinType().Build(joinType)
        .LeftJoinKeyNames(join.LeftJoinKeyNames())
        .RightJoinKeyNames(join.RightJoinKeyNames())
        .Done();
}

TExprBase DqBuildPhyJoin(const TDqJoin& join, bool pushLeftStage, TExprContext& ctx, IOptimizationContext& optCtx, bool useGraceCoreForMap, bool buildCollectStage) {
    static const std::set<std::string_view> supportedTypes = {
        "Inner"sv,
        "Left"sv,
        "Cross"sv,
        "LeftOnly"sv,
        "LeftSemi"sv
    };

    auto joinType = join.JoinType().Value();

    if (!supportedTypes.contains(joinType)) {
        return join;
    }

    TExprNode::TListType flags;
    if (const auto maybeFlags = join.Flags()) {
        flags = maybeFlags.Cast().Ref().ChildrenList();
    }

    for (auto& flag : flags) {
        if (flag->IsAtom("LeftAny") || flag->IsAtom("RightAny")) {
            ctx.AddError(TIssue(ctx.GetPosition(join.Ptr()->Pos()), "ANY join kind is not currently supported"));
            return join;
        }
    }


    YQL_ENSURE(join.LeftInput().Maybe<TDqCnUnionAll>());
    TDqCnUnionAll leftCn = join.LeftInput().Cast<TDqCnUnionAll>();

    TMaybeNode<TDqCnUnionAll> rightCn = join.RightInput().Maybe<TDqCnUnionAll>();
    YQL_ENSURE(rightCn || IsDqCompletePureExpr(join.RightInput(), /* isPrecomputePure */ true));

    TMaybeNode<TDqCnBroadcast> rightBroadcast;
    TNodeOnNodeOwnedMap rightPrecomputes;

    if (rightCn) {
        if (buildCollectStage) {
            auto collectRightStage = Build<TDqStage>(ctx, join.Pos())
                .Inputs()
                    .Add(rightCn.Cast())
                    .Build()
                .Program()
                    .Args({"stream"})
                    .Body("stream")
                    .Build()
                .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
                .Done();

            rightBroadcast = Build<TDqCnBroadcast>(ctx, join.Pos())
                .Output()
                    .Stage(collectRightStage)
                    .Index().Build("0")
                    .Build()
                .Done();
        } else {
            rightBroadcast = Build<TDqCnBroadcast>(ctx, join.Pos())
                .Output()
                    .Stage(rightCn.Cast().Output().Stage())
                    .Index(rightCn.Cast().Output().Index())
                    .Build()
                .Done();
        }
    } else {
        YQL_CLOG(TRACE, CoreDq) << "-- DqBuildPhyJoin: right input is DqPure expr";

        // right input is DqPure expression (may contain precomputes)
        VisitExpr(join.RightInput().Ptr(), [&rightPrecomputes](const TExprNode::TPtr& node) {
                if (TDqPhyPrecompute::Match(node.Get())) {
                    rightPrecomputes[node.Get()] = node;
                    return false;
                }
                return true;
            },
            [](const TExprNode::TPtr&) { return true; });

        if (rightPrecomputes.empty()) {
            // absolutely pure expression
            YQL_CLOG(TRACE, CoreDq) << "-- DqBuildPhyJoin: right input is absolutely pure expr";
        } else {
            YQL_CLOG(TRACE, CoreDq) << "-- DqBuildPhyJoin: right input is DqPure expr with " << rightPrecomputes.size()
                << " precomputes";

            if (IsDqDependsOnStage(join.RightInput(), leftCn.Output().Stage())) {
                YQL_CLOG(TRACE, CoreDq) << "-- DqBuildPhyJoin: right input is DqPure expr and depends on left side";

                TVector<TCoArgument> args; args.reserve(rightPrecomputes.size());
                TVector<TExprBase> inputs; inputs.reserve(rightPrecomputes.size());
                TNodeOnNodeOwnedMap argsReplaces;
                int i = 0;
                for (auto [raw, ptr] : rightPrecomputes) {
                    args.emplace_back(TCoArgument(ctx.NewArgument(raw->Pos(), TStringBuilder() << "precompute_" << (i++))));
                    inputs.emplace_back(ptr);
                    argsReplaces[raw] = args.back().Ptr();
                }

                auto collectRightStage = Build<TDqStage>(ctx, join.Pos())
                    .Inputs()
                        .Add(inputs)
                        .Build()
                    .Program()
                        .Args(args)
                        .Body<TCoToStream>()
                            .Input(ctx.ReplaceNodes(join.RightInput().Ptr(), argsReplaces))
                            .Build()
                        .Build()
                    .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
                    .Done();

                rightBroadcast = Build<TDqCnBroadcast>(ctx, join.Pos())
                    .Output()
                        .Stage(collectRightStage)
                        .Index().Build("0")
                        .Build()
                    .Done();
            } else {
                // do nothing
                YQL_CLOG(TRACE, CoreDq) << "-- right input is DqPure expr and doesn't depend on left side";
            }
        }
    }

    TCoArgument leftInputArg{ctx.NewArgument(join.Pos(), "_dq_join_left")};
    TCoArgument rightInputArg{ctx.NewArgument(join.Pos(), "_dq_join_right")};

    bool buildNewStage = !pushLeftStage;
    if (!rightCn) {
        // right input is DqPure expression, try to push down the join...
        if (rightPrecomputes.empty()) {
            // absolutely pure expression, don't need to create a new stage
            buildNewStage = false;
        } else {
            // right input contains precompute(s), and it may depend on left side (if rightBroadcast is defined)
            buildNewStage = rightBroadcast.IsValid();
        }
    } else if (!buildNewStage) {
        // NOTE: Can't pass data from the stage to itself.
        buildNewStage = IsDqDependsOnStage(join.RightInput(), leftCn.Output().Stage());
        if (!buildNewStage) {
            // NOTE: Do not push join to stage with multiple outputs, reduce memory footprint.
            buildNewStage = GetStageOutputsCount(leftCn.Output().Stage()) > 1;
            if (!buildNewStage && rightBroadcast) {
                // NOTE: Do not fuse additional input into stage which have first input `Broadcast` type.
                // Rule described in /ydb/library/yql/dq/tasks/dq_connection_builder.h:23
                buildNewStage = DqStageFirstInputIsBroadcast(leftCn.Output().Stage());
            }
        }
    }

    TExprBase joinRightInput = buildNewStage
        ? (TExprBase) rightInputArg
        : (rightBroadcast
            ? (TExprBase) rightBroadcast.Cast()
            : (TExprBase) Build<TCoToFlow>(ctx, join.Pos())
                .Input(join.RightInput())
                .Done());

    TMaybeNode<TExprBase> phyJoin;
    if (join.JoinType().Value() != "Cross"sv) {
        phyJoin = DqMakePhyMapJoin(join, leftInputArg, joinRightInput, ctx, useGraceCoreForMap);
    } else {
        YQL_ENSURE(join.JoinKeys().Empty());

        phyJoin = Build<TDqPhyCrossJoin>(ctx, join.Pos())
            .LeftInput(leftInputArg)
            .LeftLabel(join.LeftLabel())
            .RightInput(joinRightInput)
            .RightLabel(join.RightLabel())
            .JoinType(join.JoinType())
            .JoinKeys(join.JoinKeys())
            .LeftJoinKeyNames(join.LeftJoinKeyNames())
            .RightJoinKeyNames(join.RightJoinKeyNames())
            .Done();
    }

    TMaybeNode<TDqCnUnionAll> newConnection;
    if (buildNewStage) {
        auto newJoinStage = Build<TDqStage>(ctx, join.Pos())
            .Inputs()
                .Add<TDqCnMap>()
                    .Output(leftCn.Output())
                    .Build()
                .Add(rightBroadcast.Cast())
                .Build()
            .Program()
                .Args({leftInputArg, rightInputArg})
                .Body(phyJoin.Cast())
                .Build()
            .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
            .Done();

        newConnection = Build<TDqCnUnionAll>(ctx, join.Pos())
            .Output()
                .Stage(newJoinStage)
                .Index().Build("0")
                .Build()
            .Done();
    } else {
        auto lambda = Build<TCoLambda>(ctx, join.Pos())
            .Args({leftInputArg})
            .Body(phyJoin.Cast())
            .Done();

        TVector<TDqConnection> lambdaConnections;
        if (rightBroadcast) {
            lambdaConnections.emplace_back(rightBroadcast.Cast());
        }

        auto maybeCn = DqPushLambdaToStageUnionAll(leftCn, lambda, lambdaConnections, ctx, optCtx);
        YQL_ENSURE(maybeCn);

        auto newCn = maybeCn.Cast();
        YQL_ENSURE(newCn.Maybe<TDqCnUnionAll>());

        newConnection = newCn.Cast<TDqCnUnionAll>();
    }

    return newConnection.Cast();
}


TExprBase DqBuildJoinDict(const TDqJoin& join, TExprContext& ctx) {
    auto joinType = join.JoinType().Value();

    if (joinType != "Full"sv && joinType != "Exclusion"sv) {
        return join;
    }

    auto buildShuffle = [&ctx, &join](const TExprBase& input, const TVector<TCoAtom>& keys) {
        auto stage = Build<TDqStage>(ctx, join.Pos())
            .Inputs()
                .Add(input)
                .Build()
            .Program()
                .Args({"stream"})
                .Body("stream")
                .Build()
            .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
            .Done();

        return Build<TDqCnHashShuffle>(ctx, join.Pos())
            .Output()
                .Stage(stage)
                .Index().Build("0")
                .Build()
            .KeyColumns()
                .Add(keys)
                .Build()
            .Done();
    };

    bool leftIsUnionAll = join.LeftInput().Maybe<TDqCnUnionAll>().IsValid();
    bool rightIsUnionAll = join.RightInput().Maybe<TDqCnUnionAll>().IsValid();

    TMaybeNode<TDqStage> joinStage;

    // join streams
    if (leftIsUnionAll && rightIsUnionAll) {
        auto leftCn = join.LeftInput().Cast<TDqCnUnionAll>();
        auto rightCn = join.RightInput().Cast<TDqCnUnionAll>();

        auto [leftJoinKeys, rightJoinKeys] = GetJoinKeys(join, ctx);

        auto rightShuffle = buildShuffle(rightCn, rightJoinKeys);
        auto leftShuffle = buildShuffle(leftCn, leftJoinKeys);

        TCoArgument leftInputArg{ctx.NewArgument(join.Pos(), "_dq_join_left")};
        TCoArgument rightInputArg{ctx.NewArgument(join.Pos(), "_dq_join_right")};

        auto phyJoin = Build<TDqPhyJoinDict>(ctx, join.Pos())
            .LeftInput(leftInputArg)
            .LeftLabel(join.LeftLabel())
            .RightInput(rightInputArg)
            .RightLabel(join.RightLabel())
            .JoinType(join.JoinType())
            .JoinKeys(join.JoinKeys())
            .LeftJoinKeyNames(join.LeftJoinKeyNames())
            .RightJoinKeyNames(join.RightJoinKeyNames())
            .Done();

        joinStage = Build<TDqStage>(ctx, join.Pos())
            .Inputs()
                .Add(leftShuffle)
                .Add(rightShuffle)
                .Build()
            .Program()
                .Args({leftInputArg, rightInputArg})
                .Body(phyJoin)
                .Build()
            .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
            .Done();
    }

    // join stream with pure expr
    else if (leftIsUnionAll && IsDqCompletePureExpr(join.RightInput(), /* isPrecomputePure */ true)) {
        auto leftCn = join.LeftInput().Cast<TDqCnUnionAll>();

        auto [leftJoinKeys, _] = GetJoinKeys(join, ctx);

        auto leftShuffle = buildShuffle(leftCn, leftJoinKeys);
        TCoArgument leftInputArg{ctx.NewArgument(join.Pos(), "_dq_join_left")};

        auto phyJoin = Build<TDqPhyJoinDict>(ctx, join.Pos())
            .LeftInput(leftInputArg)
            .LeftLabel(join.LeftLabel())
            .RightInput<TCoToStream>()
                .Input(join.RightInput())
                .Build()
            .RightLabel(join.RightLabel())
            .JoinType(join.JoinType())
            .JoinKeys(join.JoinKeys())
            .LeftJoinKeyNames(join.LeftJoinKeyNames())
            .RightJoinKeyNames(join.RightJoinKeyNames())
            .Done();

        joinStage = Build<TDqStage>(ctx, join.Pos())
            .Inputs()
                .Add(leftShuffle)
                .Build()
            .Program()
                .Args({leftInputArg})
                .Body(phyJoin)
                .Build()
            .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
            .Done();
    }

    // join pure expr with stream
    else if (IsDqCompletePureExpr(join.RightInput(), /* isPrecomputePure */ true) && rightIsUnionAll) {
        auto rightCn = join.RightInput().Cast<TDqCnUnionAll>();

        auto [_, rightJoinKeys] = GetJoinKeys(join, ctx);

        auto rightShuffle = buildShuffle(rightCn, rightJoinKeys);
        TCoArgument rightInputArg{ctx.NewArgument(join.Pos(), "_dq_join_right")};

        auto phyJoin = Build<TDqPhyJoinDict>(ctx, join.Pos())
            .LeftInput(join.LeftInput())
            .LeftLabel(join.LeftLabel())
            .RightInput<TCoToStream>()
                .Input(rightInputArg)
                .Build()
            .RightLabel(join.RightLabel())
            .JoinType(join.JoinType())
            .JoinKeys(join.JoinKeys())
            .LeftJoinKeyNames(join.LeftJoinKeyNames())
            .RightJoinKeyNames(join.RightJoinKeyNames())
            .Done();

        joinStage = Build<TDqStage>(ctx, join.Pos())
            .Inputs()
                .Add(rightShuffle)
                .Build()
            .Program()
                .Args({rightInputArg})
                .Body(phyJoin)
                .Build()
            .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
            .Done();
    }

    else {
        // TODO: pure join, do nothing?
    }

    if (joinStage) {
        return Build<TDqCnUnionAll>(ctx, join.Pos())
            .Output()
                .Stage(joinStage.Cast())
                .Index().Build("0")
                .Build()
            .Done();
    }

    return join;
}

namespace {

TExprNode::TPtr ExpandJoinInput(const TStructExprType& type, TExprNode::TPtr&& arg, TExprContext& ctx) {
    return ctx.Builder(arg->Pos())
            .Callable("ExpandMap")
                .Add(0, std::move(arg))
                .Lambda(1)
                    .Param("item")
                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                        auto i = 0U;
                        for (const auto& item : type.GetItems()) {
                            parent.Callable(i++, "Member")
                                .Arg(0, "item")
                                .Atom(1, item->GetName())
                                .Seal();
                        }
                        return parent;
                    })
                .Seal()
            .Seal().Build();
}

TExprNode::TPtr SqueezeJoinInputToDict(TExprNode::TPtr&& input, size_t width, const std::vector<ui32>& keys, bool withPayloads, bool multiRow, TExprContext& ctx) {
    YQL_ENSURE(width > 0U && !keys.empty());
    return ctx.Builder(input->Pos())
        .Callable("NarrowSqueezeToDict")
            .Add(0, std::move(input))
            .Lambda(1)
                .Params("items", width)
                .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                    if (keys.size() > 1U) {
                        auto list = parent.List();
                        for (ui32 i = 0U; i < keys.size(); ++i)
                            list.Arg(i, "items", keys[i]);
                        list.Seal();
                    } else
                        parent.Arg("items", keys.front());
                    return parent;
                })
            .Seal()
            .Lambda(2)
                .Params("items", width)
                .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                    if (withPayloads)
                        parent.List().Args("items", width).Seal();
                    else
                        parent.Callable("Void").Seal();
                    return parent;
                })
            .Seal()
            .List(3)
                .Atom(0, "Hashed", TNodeFlags::Default)
                .Atom(1, withPayloads && multiRow ? "Many" : "One", TNodeFlags::Default)
            .Seal()
        .Seal().Build();
}

using TModifyKeysList = std::vector<std::tuple<TCoAtom, TCoAtom, ui32, const TTypeAnnotationNode*>>;

template<bool LeftOrRight>
TCoLambda PrepareJoinSide(
    TPositionHandle pos,
    const std::map<std::string_view, ui32>& columns,
    const std::vector<TCoAtom>& keys,
    TModifyKeysList& remap,
    bool filter,
    TExprNode::TListType& keysList,
    TExprContext& ctx) {

    TCoArgument inputArg{ctx.NewArgument(pos, "flow")};
    auto preprocess = ctx.Builder(inputArg.Pos())
        .Callable("Map")
            .Add(0, inputArg.Ptr())
            .Lambda(1)
                .Param("row")
                .Callable("AsStruct")
                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                        ui32 i = 0U;
                        for (const auto& colName : columns) {
                            parent.List(i++)
                                .Atom(0, colName.first)
                                .Callable(1, "Member")
                                    .Arg(0, "row")
                                    .Atom(1, colName.first)
                                .Seal()
                            .Seal();
                        }
                        for (const auto& key : remap) {
                            parent.List(i++)
                                .Add(0, std::get<1>(key).Ptr())
                                .Callable(1, "StrictCast")
                                    .Callable(0, "Member")
                                        .Arg(0, "row")
                                        .Add(1, std::get<0>(key).Ptr())
                                    .Seal()
                                    .Add(1, ExpandType(pos, *std::get<const TTypeAnnotationNode*>(key), ctx))
                                .Seal()
                            .Seal();
                        }
                        return parent;
                    })
                .Seal()
            .Seal()
        .Seal().Build();

    if (filter) {
        TExprNode::TListType check, unwrap;
        check.reserve(keys.size() + remap.size());
        unwrap.reserve(remap.size());
        std::transform(keys.cbegin(), keys.cend(), std::back_inserter(check), [&](const TCoAtom& key) { return key.Ptr(); });
        std::for_each(remap.cbegin(), remap.cend(), [&](const TModifyKeysList::value_type& key) {
            (ETypeAnnotationKind::Optional == std::get<const TTypeAnnotationNode*>(key)->GetKind() ? check : unwrap).emplace_back(std::get<1>(key).Ptr());
        });
        preprocess = Build<TCoSkipNullMembers>(ctx, preprocess->Pos())
            .Input(std::move(preprocess))
            .Members().Add(std::move(check)).Build()
            .Done().Ptr();
        if (!unwrap.empty()) {
            preprocess = Build<TCoFilterNullMembers>(ctx, preprocess->Pos())
                .Input(std::move(preprocess))
                .Members().Add(std::move(unwrap)).Build()
                .Done().Ptr();
        }
    }

    for (auto& key : remap) {
        const auto index = std::get<ui32>(key);
        keysList[index] = ctx.ChangeChild(*keysList[index], LeftOrRight ? TDqJoinKeyTuple::idx_LeftColumn : TDqJoinKeyTuple::idx_RightColumn, std::get<1>(key).Ptr());
    }

    return Build<TCoLambda>(ctx, preprocess->Pos())
        .Args({inputArg})
        .Body(std::move(preprocess))
        .Done();
}

TExprNode::TPtr ReplaceJoinOnSide(TExprNode::TPtr&& input, const TTypeAnnotationNode& resutType, const std::string_view& tableName, TExprContext& ctx) {
    const auto pos = input->Pos();
    const auto typeOfSide = GetSeqItemType(*input->GetTypeAnn()).Cast<TStructExprType>();
    return ctx.Builder(pos)
        .Callable("Map")
            .Add(0, std::move(input))
            .Lambda(1)
                .Param("item")
                .Callable("StrictCast")
                    .Callable(0, "AsStruct")
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            auto i = 0U;
                            for (const auto& item : typeOfSide->GetItems()) {
                                parent.List(i++)
                                    .Atom(0, tableName.empty() ? item->GetName() : FullColumnName(tableName, item->GetName()))
                                    .Callable(1, "Member")
                                        .Arg(0, "item")
                                        .Atom(1, item->GetName())
                                    .Seal()
                                .Seal();
                            }
                            return parent;
                        })
                    .Seal()
                    .Add(1, ExpandType(pos, GetSeqItemType(resutType), ctx))
                .Seal()
            .Seal()
        .Seal().Build();
}

}

TExprBase DqBuildHashJoin(
    const TDqJoin& join,
    EHashJoinMode mode,
    TExprContext& ctx,
    IOptimizationContext& optCtx,
    bool shuffleElimination,
    bool shuffleEliminationWithMap,
    bool useBlockHashJoin
) {
    const auto joinType = join.JoinType().Value();
    YQL_ENSURE(joinType != "Cross"sv);

    auto leftIn = join.LeftInput().Cast<TDqCnUnionAll>().Output();
    auto rightIn = join.RightInput().Cast<TDqCnUnionAll>().Output();

    const auto leftStructType = GetSequenceItemType(leftIn, false, ctx)->Cast<TStructExprType>();
    const auto rightStructType = GetSequenceItemType(rightIn, false, ctx)->Cast<TStructExprType>();

    const auto& leftItems = leftStructType->GetItems();
    const auto& rightItems = rightStructType->GetItems();

    std::map<std::string_view, ui32> leftNames;
    for (ui32 i = 0; i < leftItems.size(); i++) {
        leftNames.emplace(leftItems[i]->GetName(), i);
    }

    std::map<std::string_view, ui32> rightNames;
    for (ui32 i = 0; i < rightItems.size(); i++) {
        rightNames.emplace(rightItems[i]->GetName(), i);
    }

    const auto [leftJoinKeys, rightJoinKeys] = GetJoinKeys(join, ctx);
    YQL_ENSURE(leftJoinKeys.size() == rightJoinKeys.size());

    bool badKey = false;
    const bool filter = joinType == "Inner"sv || joinType.ends_with("Semi");
    const bool leftKind = joinType.starts_with("Left"sv);
    const bool rightKind = joinType.starts_with("Right"sv);
    TModifyKeysList remapLeft, remapRight;
    bool shuffleLeftSide = !join.ShuffleLeftSideBy() || !join.ShuffleLeftSideBy().Cast().Empty() || !shuffleElimination;
    bool shuffleRightSide = !join.ShuffleRightSideBy() || !join.ShuffleRightSideBy().Cast().Empty() || !shuffleElimination;
    THashMap<TString, TString> leftColumnRemap;
    THashMap<TString, TString> rightColumnRemap;
    if (shuffleLeftSide && shuffleRightSide /* for columnshardhashv1 (shuffle elimination) it is important to save original types for join predicate */) {
        for (ui32 i = 0U; i < rightJoinKeys.size() && !badKey; ++i) {
            const auto keyType1 = leftStructType->FindItemType(leftJoinKeys[i]);
            const auto keyType2 = rightStructType->FindItemType(rightJoinKeys[i]);
            YQL_ENSURE(keyType1 && keyType2, "Missed key column.");
            const TTypeAnnotationNode* commonType = nullptr;
            if (leftKind) {
                commonType = JoinDryKeyType(!filter, keyType1, keyType2, ctx);
            } else if (rightKind){
                commonType = JoinDryKeyType(!filter, keyType2, keyType1, ctx);
            } else {
                commonType = JoinCommonDryKeyType(join.Pos(), !filter, keyType1, keyType2, ctx);
            }

            if (commonType) {
                if (!IsSameAnnotation(*keyType1, *commonType)) {
                    TString rename = (TString("_yql_dq_key_left_") + ToString(i));
                    leftColumnRemap[leftJoinKeys[i].StringValue()] = rename;
                    remapLeft.emplace_back(leftJoinKeys[i], ctx.NewAtom(leftJoinKeys[i].Pos(), std::move(rename), TNodeFlags::Default), i, commonType);
                }
                if (!IsSameAnnotation(*keyType2, *commonType)) {
                    TString rename = TString("_yql_dq_key_right_") + ToString(i);
                    rightColumnRemap[rightJoinKeys[i].StringValue()] = rename;
                    remapRight.emplace_back(rightJoinKeys[i], ctx.NewAtom(rightJoinKeys[i].Pos(), rename, TNodeFlags::Default), i, commonType);
                }
            } else
                badKey = true;
        }
    }

    const bool singleSide = joinType.ends_with("Semi"sv) || joinType.ends_with("Only"sv);
    std::string_view leftTableName;
    if (join.LeftLabel().Ref().IsAtom()) {
        leftTableName = join.LeftLabel().Cast<TCoAtom>().Value();
    }

    std::string_view rightTableName;
    if (join.RightLabel().Ref().IsAtom()) {
        rightTableName = join.RightLabel().Cast<TCoAtom>().Value();
    }

    if (badKey) {
        const auto resultType = join.Ref().GetTypeAnn();
        if (filter) {
            return TExprBase(ctx.NewCallable(join.Pos(), GetEmptyCollectionName(join.Ref().GetTypeAnn()), {ExpandType(join.Pos(), *resultType, ctx)}));
        } else if (leftKind) {
            return TExprBase(ReplaceJoinOnSide(join.LeftInput().Ptr(), *resultType, leftTableName, ctx));
        } else if (rightKind) {
            return TExprBase(ReplaceJoinOnSide(join.RightInput().Ptr(), *resultType, rightTableName, ctx));
        } else {
            return TExprBase(ctx.NewCallable(join.Pos(), "Extend", {
                ReplaceJoinOnSide(join.RightInput().Ptr(), *resultType, rightTableName, ctx),
                ReplaceJoinOnSide(join.LeftInput().Ptr(), *resultType, leftTableName, ctx)
            }));
        }
    }

    auto buildNewStage = [&](TCoLambda remapLambda, TDqCnUnionAll& conn) {
        auto collectStage = Build<TDqStage>(ctx, conn.Pos())
            .Inputs()
                .Add(conn)
                .Build()
            .Program(remapLambda)
            .Settings(TDqStageSettings().BuildNode(ctx, conn.Pos()))
            .Done();

        conn = Build<TDqCnUnionAll>(ctx, conn.Pos())
            .Output()
                .Stage(collectStage)
                .Index().Build(0U)
                .Build()
            .Done().Cast<TDqCnUnionAll>();
    };

    if (!remapLeft.empty() || !remapRight.empty()) {
        auto joinKeys = join.JoinKeys().Ref().ChildrenList();
        auto connLeft = join.LeftInput().Cast<TDqCnUnionAll>();
        auto connRight = join.RightInput().Cast<TDqCnUnionAll>();

        std::vector<std::pair<TDqCnUnionAll, TCoLambda>> remaps;
        bool canPushLeftLambdaToStage = false;
        bool canPushRightLambdaToStage = false;

        if (!remapLeft.empty()) {
            auto lambda = PrepareJoinSide<true>(connLeft.Pos(), leftNames, leftJoinKeys, remapLeft, filter || rightKind, joinKeys, ctx);
            if (!IsDqDependsOnStageOutput(join.RightInput(), connLeft.Output().Stage(), FromString<ui32>(connLeft.Output().Index().Value()))) {
                remaps.emplace_back(connLeft, std::move(lambda));
                canPushLeftLambdaToStage = true;
            } else {
                buildNewStage(std::move(lambda), connLeft);
            }
        }

        if (!remapRight.empty()) {
            auto lambda = PrepareJoinSide<false>(connRight.Pos(), rightNames, rightJoinKeys, remapRight, filter || leftKind, joinKeys, ctx);
            if (!IsDqDependsOnStageOutput(join.RightInput(), connLeft.Output().Stage(), FromString<ui32>(connLeft.Output().Index().Value()))) {
                remaps.emplace_back(connRight, std::move(lambda));
                canPushRightLambdaToStage = true;
            } else {
                buildNewStage(std::move(lambda), connRight);
            }
        }

        if (!remaps.empty()) {
            DqPushLambdasToStagesUnionAll(remaps, ctx, optCtx);
            connLeft = canPushLeftLambdaToStage ? remaps.front().first : connLeft;
            connRight = canPushRightLambdaToStage ? remaps.back().first : connRight;
        }

        const auto& items = GetSeqItemType(*join.Ref().GetTypeAnn()).Cast<TStructExprType>()->GetItems();
        TExprNode::TListType fields(items.size());
        std::transform(items.cbegin(), items.cend(), fields.begin(), [&](const TItemExprType* item) { return ctx.NewAtom(join.Pos(), item->GetName()); });

        auto remapShufflings =
            [&](const TMaybeNode<TExprList>& maybeShuffleBy, const THashMap<TString, TString>& columnRemap) -> TExprNode::TListType {
                if (!maybeShuffleBy) {
                    return {};
                }

                TExprNode::TListType shuffleBy;
                shuffleBy.reserve(maybeShuffleBy.Cast().Size());

                for (const auto& columnExprBase: maybeShuffleBy.Cast()) {
                    auto columnExpr = columnExprBase.Ptr();
                    TString rel;
                    TString attr;
                    if (columnExpr->ChildrenSize() == 1) {
                        attr = TString(columnExpr->Child(0)->Content());
                    } else if (columnExpr->ChildrenSize() == 2) {
                        rel = TString(columnExpr->Child(0)->Content());
                        attr = TString(columnExpr->Child(1)->Content());
                    }

                    TString columnName;
                    if (columnRemap.contains(rel + "." + attr)) {
                        columnName = columnRemap.at(rel + "." + attr);
                    } else if (columnRemap.contains(attr)) {
                        columnName = columnRemap.at(attr);
                    }

                    if (columnName) {
                        auto node =
                            ctx.Builder(join.Pos())
                                .List()
                                    .Atom(0, columnName)
                                .Seal()
                            .Build();


                        shuffleBy.push_back(std::move(node));
                        continue;
                    }

                    shuffleBy.push_back(columnExpr);
                }

                return shuffleBy;
            };

        auto dqJoin =
            Build<TDqJoin>(ctx, join.Pos())
                .InitFrom(join)
                .LeftInput(connLeft)
                .RightInput(connRight)
                .JoinKeys(ctx.ChangeChildren(join.JoinKeys().Ref(), std::move(joinKeys)));

        if (auto shuffleLeftSideBy = remapShufflings(join.ShuffleLeftSideBy(), leftColumnRemap); !shuffleLeftSideBy.empty()) {
            dqJoin
                .ShuffleLeftSideBy()
                    .Add(std::move(shuffleLeftSideBy))
                .Build();
        }
        if (auto shuffleRightSideBy = remapShufflings(join.ShuffleRightSideBy(), rightColumnRemap); !shuffleRightSideBy.empty()) {
            dqJoin
                .ShuffleRightSideBy()
                    .Add(std::move(shuffleRightSideBy))
                .Build();
        }

        return
            Build<TCoExtractMembers>(ctx, join.Pos())
                .Input(std::move(dqJoin.Build().Value()))
                .Members()
                    .Add(std::move(fields))
                .Build()
            .Done();
    }

    std::vector<ui32> leftKeys, rightKeys;
    std::transform(leftJoinKeys.cbegin(), leftJoinKeys.cend(), std::back_inserter(leftKeys), [&](const std::string_view& name) { return leftNames[name]; });
    std::transform(rightJoinKeys.cbegin(), rightJoinKeys.cend(), std::back_inserter(rightKeys), [&](const std::string_view& name) { return rightNames[name]; });

    const auto buildShuffle = [&ctx, &join](const TDqOutput& input, const TVector<TCoAtom>& keys) {
        return Build<TDqCnHashShuffle>(ctx, join.Pos())
                .Output(input)
                .KeyColumns()
                    .Add(keys)
                    .Build()
                .UseSpilling().Build(true)
                .Done().Ptr();
    };

    auto buildShuffleKeys = [&ctx, &join](const TExprList& exprList, const TVector<TCoAtom>& joinKeys) -> TVector<TCoAtom> {
        Y_ENSURE(exprList.Size() <= joinKeys.size());

        auto contains = [&joinKeys](const TString& column){
            return std::find_if(
                joinKeys.begin(),
                joinKeys.end(),
                [&column](const TCoAtom& atom){ return atom.StringValue() == column; }
            ) != joinKeys.end();
        };

        TVector<TCoAtom> atomVector;
        atomVector.reserve(exprList.Size());
        for (std::size_t i = 0; i < exprList.Size(); ++i) {
            TString rel, attr;
            auto exprItem = exprList.Item(i).Ptr();
            if (exprItem->ChildrenSize() == 1) {
                attr = TString(exprItem->Child(0)->Content());
            } else if (exprItem->ChildrenSize() == 2) {
                rel  = TString(exprItem->Child(0)->Content());
                attr = TString(exprItem->Child(1)->Content());
            }

            TString column;
            if (contains(rel + "." + attr)){
                column = rel + "." + attr;
            } else if (contains(attr)) {
                column = std::move(attr);
            } else {
                Y_ENSURE(false, TStringBuilder{} << "There's no such column for shuffling: " <<  "." << attr);
            }

            auto atom =
                Build<TCoAtom>(ctx, join.Pos())
                    .Value(std::move(column))
                .Done();

            atomVector.push_back(std::move(atom));
        }

        return atomVector;
    };

    const auto buildMap = [&ctx, &join](TDqOutput& input) {
        return
            Build<TDqCnMap>(ctx, join.Pos())
                .Output(input)
                .Done().Ptr();
    };

    TExprNode::TPtr leftConnection;
    if (!join.ShuffleLeftSideBy()) {
        YQL_CLOG(TRACE, CoreDq) << "ShuffleLeftSide isn't defined";
    }
    if (shuffleLeftSide) {
        if (shuffleElimination && join.ShuffleLeftSideBy()) {
            leftConnection = buildShuffle(
                leftIn,
                buildShuffleKeys(join.ShuffleLeftSideBy().Cast(), leftJoinKeys)
            );
        } else {
            leftConnection = buildShuffle(
                leftIn,
                leftJoinKeys
            );
        }
    } else {
        leftConnection = buildMap(leftIn);
    }

    TExprNode::TPtr rightConnection;
    if (!join.ShuffleRightSideBy()) {
        YQL_CLOG(TRACE, CoreDq) << "ShuffleRightSide isn't defined";
    }
    if (shuffleRightSide) {
        if (shuffleElimination && join.ShuffleRightSideBy()) {
            rightConnection = buildShuffle(
                rightIn,
                buildShuffleKeys(join.ShuffleRightSideBy().Cast(),  rightJoinKeys)
            );
        } else {
            rightConnection = buildShuffle(
                rightIn,
                rightJoinKeys
            );
        }
    } else {
        rightConnection = buildMap(rightIn);
    }

    TString callableName = "GraceJoinCore";
    int shift = 2;
    bool selfJoin = false;
    if (mode == EHashJoinMode::GraceAndSelf && leftIn.Stage().Ptr() == rightIn.Stage().Ptr()) {
        callableName = "GraceSelfJoinCore";
        shift = 1;
        selfJoin = true;
    }

    TCoArgument leftInputArg{ctx.NewArgument(join.LeftInput().Pos(), "_dq_join_left")};
    TCoArgument rightInputArg{ctx.NewArgument(join.RightInput().Pos(), "_dq_join_right")};

    auto leftWideFlow = ExpandJoinInput(*leftStructType, leftInputArg.Ptr(), ctx);
    auto rightWideFlow = ExpandJoinInput(*rightStructType, rightInputArg.Ptr(), ctx);

    const auto leftFullWidth = leftNames.size();
    const auto rightFullWidth = rightNames.size();

    if (singleSide && rightKind)
        leftNames.clear();
    if (singleSide && leftKind)
        rightNames.clear();

    TExprNode::TListType flags;
    if (const auto maybeJoin = join.Maybe<TDqJoin>()) {
        if (const auto maybeFlags = maybeJoin.Cast().Flags()) {
            flags = maybeFlags.Cast().Ref().ChildrenList();
        }
    }

    TExprNode::TPtr hashJoin;
    switch (mode) {
        case EHashJoinMode::GraceAndSelf:
        case EHashJoinMode::Grace:
            if (useBlockHashJoin) {
                // Create TDqPhyBlockHashJoin node with structured inputs - peephole will handle conversion
                // Pass the original structured inputs, not wide flows
                hashJoin = Build<TDqPhyBlockHashJoin>(ctx, join.Pos())
                    .LeftInput(leftInputArg)
                    .RightInput(rightInputArg)
                    .LeftLabel(join.LeftLabel())
                    .RightLabel(join.RightLabel())
                    .JoinType(join.JoinType())
                    .JoinKeys(join.JoinKeys())
                    .LeftJoinKeyNames(join.LeftJoinKeyNames())
                    .RightJoinKeyNames(join.RightJoinKeyNames())
                    .Done().Ptr();
                break;
            }

            hashJoin = ctx.Builder(join.Pos())
                .Callable(callableName)
                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                        parent.Add(0, std::move(leftWideFlow));
                        if (selfJoin == false) {
                            parent.Add(1, std::move(rightWideFlow));
                        }
                        return parent;
                    })
                    .Add(shift, join.JoinType().Ptr())
                    .List(shift + 1)
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            for (ui32 i = 0U; i < leftKeys.size(); ++i) {
                                parent.Atom(i, ctx.GetIndexAsString(leftKeys[i]), TNodeFlags::Default);
                            }
                            return parent;
                        })
                    .Seal()
                    .List(shift + 2)
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            for (ui32 i = 0U; i < rightKeys.size(); ++i) {
                                parent.Atom(i, ctx.GetIndexAsString(rightKeys[i]), TNodeFlags::Default);
                            }
                            return parent;
                        })
                    .Seal()
                    .List(shift + 3)
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            for (ui32 i = 0U; i < leftNames.size(); ++i) {
                                parent.Atom(2*i, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                parent.Atom(2*i + 1, ctx.GetIndexAsString(i), TNodeFlags::Default);
                            }
                            return parent;
                        })
                    .Seal()
                    .List(shift + 4)
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            for (ui32 i = 0U; i < rightNames.size(); ++i) {
                                parent.Atom(2*i, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                parent.Atom(2*i + 1, ctx.GetIndexAsString(leftNames.size() + i), TNodeFlags::Default);
                            }
                            return parent;
                        })
                    .Seal()
                    .List(shift + 5).Add(join.LeftJoinKeyNames().Ref().ChildrenList()).Seal()
                    .List(shift + 6).Add(join.RightJoinKeyNames().Ref().ChildrenList()).Seal()
                    .List(shift + 7).Add(std::move(flags)).Seal()
                .Seal()
                .Build();
            break;
        case EHashJoinMode::Map:
            if (leftKind || joinType == "Inner"sv) {
                hashJoin = ctx.Builder(join.Pos())
                    .Callable("FlatMap")
                        .Add(0, SqueezeJoinInputToDict(std::move(rightWideFlow), rightFullWidth, rightKeys, !rightNames.empty(), true, ctx))
                        .Lambda(1)
                            .Param("dict")
                            .Callable("MapJoinCore")
                                .Add(0, std::move(leftWideFlow))
                                .Arg(1, "dict")
                                .Add(2, join.JoinType().Ptr())
                                .List(3)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < leftKeys.size(); ++i) {
                                            parent.Atom(i, ctx.GetIndexAsString(leftKeys[i]), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(4)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < rightKeys.size(); ++i) {
                                            parent.Atom(i, ctx.GetIndexAsString(rightKeys[i]), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(5)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < leftNames.size(); ++i) {
                                            parent.Atom(2*i, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                            parent.Atom(2*i + 1, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(6)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < rightNames.size(); ++i) {
                                            parent.Atom(2*i, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                            parent.Atom(2*i + 1, ctx.GetIndexAsString(leftNames.size() + i), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(7).Add(join.LeftJoinKeyNames().Ref().ChildrenList()).Seal()
                                .List(8).Add(join.RightJoinKeyNames().Ref().ChildrenList()).Seal()
                            .Seal()
                        .Seal()
                    .Seal().Build();
                break;
            } else if (rightKind) {
                hashJoin = ctx.Builder(join.Pos())
                    .Callable("FlatMap")
                        .Add(0, SqueezeJoinInputToDict(std::move(leftWideFlow), leftFullWidth, leftKeys, !leftNames.empty(), true, ctx))
                        .Lambda(1)
                            .Param("dict")
                            .Callable("MapJoinCore")
                                .Add(0, std::move(rightWideFlow))
                                .Arg(1, "dict")
                                .Atom(2, TString("Left") += joinType.substr(5U), TNodeFlags::Default)
                                .List(3)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < rightKeys.size(); ++i) {
                                            parent.Atom(i, ctx.GetIndexAsString(rightKeys[i]), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(4)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < leftKeys.size(); ++i) {
                                            parent.Atom(i, ctx.GetIndexAsString(leftKeys[i]), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(5)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < rightNames.size(); ++i) {
                                            parent.Atom(2*i, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                            parent.Atom(2*i + 1, ctx.GetIndexAsString(i + leftNames.size()), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(6)
                                    .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                                        for (ui32 i = 0U; i < leftNames.size(); ++i) {
                                            parent.Atom(2*i, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                            parent.Atom(2*i + 1, ctx.GetIndexAsString(i), TNodeFlags::Default);
                                        }
                                        return parent;
                                    })
                                .Seal()
                                .List(7).Add(join.LeftJoinKeyNames().Ref().ChildrenList()).Seal()
                                .List(8).Add(join.RightJoinKeyNames().Ref().ChildrenList()).Seal()
                            .Seal()
                        .Seal()
                    .Seal().Build();
                break;
            }
            [[fallthrough]];
        case EHashJoinMode::Dict: {
            bool leftAny = false, rightAny = false;
            for (auto& flag : flags) {
                if (flag->IsAtom("LeftAny")) {
                    leftAny = true;
                    flag = ctx.NewAtom(flag->Pos(), "LeftUnique", TNodeFlags::Default);
                } else if (flag->IsAtom("RightAny")) {
                    rightAny = true;
                    flag = ctx.NewAtom(flag->Pos(), "RightUnique", TNodeFlags::Default);
                }
            }

            hashJoin = ctx.Builder(join.Pos())
                .Callable("ExpandMap")
                    .Callable(0, "FlatMap")
                        .Add(0, SqueezeJoinInputToDict(std::move(leftWideFlow), leftFullWidth, leftKeys, !leftNames.empty(), !leftAny, ctx))
                        .Lambda(1)
                            .Param("left")
                            .Callable("FlatMap")
                                .Add(0, SqueezeJoinInputToDict(std::move(rightWideFlow), rightFullWidth, rightKeys, !rightNames.empty(), !rightAny, ctx))
                                .Lambda(1)
                                    .Param("right")
                                    .Callable("JoinDict")
                                        .Arg(0, "left")
                                        .Arg(1, "right")
                                        .Add(2, join.JoinType().Ptr())
                                        .List(3).Add(std::move(flags)).Seal()
                                    .Seal()
                                .Seal()
                            .Seal()
                        .Seal()
                    .Seal()
                    .Lambda(1)
                        .Param("out")
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            auto j = 0U;
                            if (singleSide) {
                                for (auto i = 0U; i < leftNames.size(); ++i) {
                                    parent.Callable(j++, "Nth")
                                            .Arg(0, "out")
                                            .Atom(1, ctx.GetIndexAsString(i), TNodeFlags::Default)
                                        .Seal();
                                }
                                for (auto i = 0U; i < rightNames.size(); ++i) {
                                    parent.Callable(j++, "Nth")
                                            .Arg(0, "out")
                                            .Atom(1, ctx.GetIndexAsString(i), TNodeFlags::Default)
                                        .Seal();
                                }
                            } else {
                                for (auto i = 0U; i < leftNames.size(); ++i) {
                                    parent.Callable(j++, "Nth")
                                            .Callable(0, "Nth")
                                                .Arg(0, "out")
                                                .Atom(1, ctx.GetIndexAsString(0), TNodeFlags::Default)
                                            .Seal()
                                            .Atom(1, ctx.GetIndexAsString(i), TNodeFlags::Default)
                                        .Seal();
                                }
                                for (auto i = 0U; i < rightNames.size(); ++i) {
                                    parent.Callable(j++, "Nth")
                                            .Callable(0, "Nth")
                                                .Arg(0, "out")
                                                .Atom(1, ctx.GetIndexAsString(1), TNodeFlags::Default)
                                            .Seal()
                                            .Atom(1, ctx.GetIndexAsString(i), TNodeFlags::Default)
                                        .Seal();
                                }
                            }
                            return parent;
                        })
                    .Seal()
                .Seal().Build();
        }   break;
        default:
            ythrow yexception() << "Invalid hash join mode: " << mode;
    }

    if (!useBlockHashJoin) {
        std::vector<TString> fullColNames;
        for (const auto& v: leftNames) {
            if (leftTableName.empty()) {
                fullColNames.emplace_back(v.first);
            } else {
                fullColNames.emplace_back(FullColumnName(leftTableName, v.first));
            }
        }

        for (const auto& v: rightNames ) {
            if (rightTableName.empty()) {
                fullColNames.emplace_back(v.first);
            } else {
                fullColNames.emplace_back(FullColumnName(rightTableName, v.first));
            }
        }

        hashJoin = ctx.Builder(join.Pos())
            .Callable("NarrowMap")
                .Add(0, std::move(hashJoin))
                .Lambda(1)
                    .Params("output", fullColNames.size())
                    .Callable("AsStruct")
                        .Do([&](TExprNodeBuilder& parent) -> TExprNodeBuilder& {
                            ui32 i = 0U;
                            for (const auto& colName : fullColNames) {
                                parent.List(i)
                                    .Atom(0, colName)
                                    .Arg(1, "output", i)
                                .Seal();
                                i++;
                            }
                            return parent;
                        })
                    .Seal()
                .Seal()
            .Seal()
            .Build();
    }

    // this func add join to the stage and add connection to it. we do this instead of map connection to reduce data network interacting
    auto addJoinToStage =
    [&ctx, &hashJoin, &join, &leftInputArg, &rightInputArg](const auto& stage, const TExprNode::TPtr& connection, bool stageSideIsLeft) {
        const auto& program = stage.Program();
        YQL_ENSURE(program.Args().Size() == stage.Inputs().Size());

        TVector<TExprBase> stageInputs;
        stageInputs.reserve(program.Args().Size());
        TVector<TCoArgument> inputArgs;
        inputArgs.reserve(program.Args().Size());

        size_t argIndex = 0;
        TNodeOnNodeOwnedMap leftReplaces(program.Args().Size() + 1);
        for (size_t i = 0; i < stage.Inputs().Size(); i++) {
            const auto input = stage.Inputs().Item(i);
            TCoArgument newArg{ctx.NewArgument(join.Pos(), TStringBuilder() << "_dq_join_fuse_" << argIndex++)};
            YQL_ENSURE(leftReplaces.emplace(program.Args().Arg(i).Raw(), newArg.Ptr()).second);
            inputArgs.emplace_back(newArg);
            stageInputs.push_back(input);
        }
        auto body = ctx.ReplaceNodes(program.Body().Ptr(), leftReplaces);
        if (TCoFromFlow::Match(body.Get())) {
            body = TExprNode::TPtr(&body->Head());
        }

        TCoArgument newArg{ ctx.NewArgument(join.Pos(), "_dq_join_fuse") };
        inputArgs.push_back(newArg);
        stageInputs.push_back(TExprBase(connection));

        TNodeOnNodeOwnedMap joinReplaces(2);
        if (stageSideIsLeft) {
            joinReplaces.emplace(leftInputArg.Raw(), body);
            joinReplaces.emplace(rightInputArg.Raw(), newArg.Ptr());
        } else {
            joinReplaces.emplace(leftInputArg.Raw(), newArg.Ptr());
            joinReplaces.emplace(rightInputArg.Raw(), body);
        }

        auto newBody = ctx.ReplaceNodes(std::move(hashJoin), joinReplaces);

        return Build<TDqCnUnionAll>(ctx, join.Pos())
            .Output()
                .Stage<TDqStage>()
                    .Inputs()
                        .Add(stageInputs)
                        .Build()
                    .Program()
                        .Args(inputArgs)
                        .Body(std::move(newBody))
                        .Build()
                    .Settings(TDqStageSettings().SetShuffleEliminated().BuildNode(ctx, join.Pos()))
                    .Build()
                .Index().Build(ctx.GetIndexAsString(0), TNodeFlags::Default)
                .Build()
            .Done();
    };

    if (!shuffleEliminationWithMap && !leftIn.Stage().Program().Body().Maybe<TDqReplicate>() && !shuffleLeftSide && shuffleRightSide) {
        return addJoinToStage(leftIn.Stage(), rightConnection, true);
    }

    if (!shuffleEliminationWithMap && !rightIn.Stage().Program().Body().Maybe<TDqReplicate>() && shuffleLeftSide && !shuffleRightSide) {
        return addJoinToStage(rightIn.Stage(), leftConnection, false);
    }

    TVector<TExprBase> stageInputs; stageInputs.reserve(2);
    stageInputs.emplace_back(leftConnection);
    if (selfJoin == false) {
        stageInputs.emplace_back(rightConnection);
    }
    TVector<TCoArgument> inputArgs; inputArgs.reserve(2);
    inputArgs.emplace_back(leftInputArg);
    if (selfJoin == false) {
        inputArgs.emplace_back(rightInputArg);
    }

    return Build<TDqCnUnionAll>(ctx, join.Pos())
        .Output()
            .Stage<TDqStage>()
                .Inputs()
                    .Add(stageInputs)
                    .Build()
                .Program()
                    .Args(inputArgs)
                    .Body(std::move(hashJoin))
                    .Build()
                .Settings(TDqStageSettings().BuildNode(ctx, join.Pos()))
                .Build()
            .Index().Build(ctx.GetIndexAsString(0), TNodeFlags::Default)
            .Build()
        .Done();
}

} // namespace NYql::NDq
