LIBRARY()

SRCS(
    abstract.cpp
    read_metadata.cpp
    constructor.cpp
    read_context.cpp
)

PEERDIR(
    ydb/core/tx/columnshard/engines/scheme/versions
    ydb/core/tx/program
    ydb/core/protos
    ydb/core/tx/columnshard/data_sharing/protos
    ydb/core/tx/conveyor/usage
)

GENERATE_ENUM_SERIALIZATION(read_metadata.h)
YQL_LAST_ABI_VERSION()

END()
