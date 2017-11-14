from urllib.parse import urlparse, parse_qsl

from .log import logger

_NOTSET = object()


# NOTE: never put here anything else;
#       just this basic types
_converters = {
    bytearray: lambda val: val,
    bytes: lambda val: val,
    float: lambda val: str(val).encode('utf-8'),
    int: lambda val: str(val).encode('utf-8'),
    str: lambda val: val.encode('utf-8'),
}


def _bytes_len(sized):
    return str(len(sized)).encode('utf-8')


def encode_command(*args):
    """Encodes arguments into redis bulk-strings array.

    Raises TypeError if any of args not of bytearray, bytes, float, int, or str
    type.
    """
    buf = bytearray()

    def add(data):
        return buf.extend(data + b'\r\n')

    add(b'*' + _bytes_len(args))
    for arg in args:
        if type(arg) in _converters:
            barg = _converters[type(arg)](arg)
            add(b'$' + _bytes_len(barg))
            add(barg)
        else:
            raise TypeError("Argument {!r} expected to be of bytearray, bytes,"
                            " float, int, or str type".format(arg))
    return buf


def decode(obj, encoding):
    if isinstance(obj, bytes):
        return obj.decode(encoding)
    elif isinstance(obj, list):
        return [decode(o, encoding) for o in obj]
    return obj


async def wait_ok(fut):
    res = await fut
    if res in (b'QUEUED', 'QUEUED'):
        return res
    return res in (b'OK', 'OK')


async def wait_convert(fut, type_, **kwargs):
    result = await fut
    if result in (b'QUEUED', 'QUEUED'):
        return result
    return type_(result, **kwargs)


async def wait_make_dict(fut):
    res = await fut
    if res in (b'QUEUED', 'QUEUED'):
        return res
    it = iter(res)
    return dict(zip(it, it))


class coerced_keys_dict(dict):

    def __getitem__(self, other):
        if not isinstance(other, bytes):
            other = _converters[type(other)](other)
        return dict.__getitem__(self, other)

    def __contains__(self, other):
        if not isinstance(other, bytes):
            other = _converters[type(other)](other)
        return dict.__contains__(self, other)


class _BaseScanIter:
    __slots__ = ('_scan', '_cur', '_ret')

    def __init__(self, scan):
        self._scan = scan
        self._cur = b'0'
        self._ret = []

    def __aiter__(self):
        return self


class _ScanIter(_BaseScanIter):

    async def __anext__(self):
        while not self._ret and self._cur:
            self._cur, self._ret = await self._scan(self._cur)
        if not self._cur and not self._ret:
            raise StopAsyncIteration  # noqa
        else:
            ret = self._ret.pop(0)
            return ret


class _ScanIterPairs(_BaseScanIter):

    async def __anext__(self):
        while not self._ret and self._cur:
            self._cur, ret = await self._scan(self._cur)
            self._ret = list(zip(ret[::2], ret[1::2]))
        if not self._cur and not self._ret:
            raise StopAsyncIteration  # noqa
        else:
            ret = self._ret.pop(0)
            return ret


def _set_result(fut, result, *info):
    if fut.done():
        logger.debug("Waiter future is already done %r %r", fut, info)
        assert fut.cancelled(), (
            "waiting future is in wrong state", fut, result, info)
    else:
        fut.set_result(result)


def _set_exception(fut, exception):
    if fut.done():
        logger.debug("Waiter future is already done %r", fut)
        assert fut.cancelled(), (
            "waiting future is in wrong state", fut, exception)
    else:
        fut.set_exception(exception)


def parse_url(url):
    """Parse Redis connection URI.

    Parse according to IANA specs:
    * https://www.iana.org/assignments/uri-schemes/prov/redis
    * https://www.iana.org/assignments/uri-schemes/prov/rediss

    Also more rules applied:

    * empty scheme is treated as unix socket path no further parsing is done.

    * 'unix://' scheme is treated as unix socket path and parsed.

    * Multiple query parameter values and blank values are considered error.

    * DB number specified as path and as query parameter is considered error.

    * Password specified in userinfo and as query parameter is
      considered error.
    """
    r = urlparse(url)

    assert r.scheme in ('', 'redis', 'rediss', 'unix'), (
        "Unsupported URI scheme", r.scheme)
    if r.scheme == '':
        return url, {}
    query = {}
    for p, v in parse_qsl(r.query, keep_blank_values=True):
        assert p not in query, ("Multiple parameters are not allowed", p, v)
        assert v, ("Empty parameters are not allowed", p, v)
        query[p] = v

    if r.scheme == 'unix':
        assert r.path, ("Empty path is not allowed", url)
        assert not r.netloc, (
            "Netlocation is not allowed for unix scheme", r.netloc)
        return r.path, _parse_uri_options(query, '', r.password)

    address = (r.hostname or 'localhost', int(r.port or 6379))
    path = r.path
    if path.startswith('/'):
        path = r.path[1:]
    options = _parse_uri_options(query, path, r.password)
    if r.scheme == 'rediss':
        options['ssl'] = True
    return address, options


def _parse_uri_options(params, path, password):

    def parse_db_num(val):
        if not val:
            return
        assert val.isdecimal(), ("Invalid decimal integer", val)
        assert val == '0' or not val.startswith('0'), (
            "Expected integer without leading zeroes", val)
        return int(val)

    options = {}

    db1 = parse_db_num(path)
    db2 = parse_db_num(params.get('db'))
    assert db1 is None or db2 is None, (
            "Single DB value expected, got path and query", db1, db2)
    if db1 is not None:
        options['db'] = db1
    elif db2 is not None:
        options['db'] = db2

    password2 = params.get('password')
    assert not password or not password2, (
            "Single password value is expected, got in net location and query")
    if password:
        options['password'] = password
    elif password2:
        options['password'] = password2

    if 'encoding' in params:
        options['encoding'] = params['encoding']
    if 'ssl' in params:
        assert params['ssl'] in ('true', 'false'), (
                "Expected 'ssl' param to be 'true' or 'false' only",
                params['ssl'])
        options['ssl'] = params['ssl'] == 'true'

    if 'timeout' in params:
        options['timeout'] = float(params['timeout'])
    return options
