PY3_LIBRARY()

LICENSE(Service-Py23-Proxy)

VERSION(Service-proxy-version)

SUBSCRIBER(g:python-contrib)

PEERDIR(contrib/python/pydantic/pydantic-1)

NO_LINT()

END()

RECURSE(
    pydantic-1
    pydantic-2
)
