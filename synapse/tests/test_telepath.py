import os
import ssl
import socket
import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

import synapse.exc as s_exc
import synapse.glob as s_glob
import synapse.common as s_common
import synapse.daemon as s_daemon
import synapse.telepath as s_telepath

import synapse.lib.cell as s_cell
import synapse.lib.coro as s_coro
import synapse.lib.share as s_share
import synapse.lib.version as s_version

import synapse.tests.utils as s_t_utils
from synapse.tests.utils import alist

class Boom:
    pass

class CustomShare(s_share.Share):
    typename = 'customshare'

    async def _runShareLoop(self):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    def boo(self, x):
        return x

    async def custgenr(self, n):
        for i in range(n):
            yield i

class Beep:
    def __init__(self, path):
        self.path = path

    def beep(self):
        return f'{self.path}: beep'

class Foo:

    def __init__(self):
        self.sleepg_evt = asyncio.Event()

    def bar(self, x, y):
        return x + y

    def baz(self, x, y):
        raise ValueError('derp')

    def echo(self, x):
        return x

    def speed(self):
        return

    async def sleepg(self, t=60):
        self.sleepg_evt.clear()
        yield ('init', {})
        try:
            await asyncio.sleep(t)
        except asyncio.CancelledError:
            self.sleepg_evt.set()
            raise
        yield ('fini', {})

    def genr(self):
        yield 10
        yield 20
        yield 30

    def genrboom(self):
        yield 10
        yield 20
        raise s_exc.SynErr(mesg='derp')

    def raze(self):
        # test that SynErr makes it through
        raise s_exc.NoSuchMeth(name='haha')

    async def corovalu(self, x, y):
        return x * 2 + y

    async def longasync(self):
        await asyncio.sleep(5)
        return 42

    async def corogenr(self, x):
        for i in range(x):
            yield i
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return

    def boom(self):
        return Boom()


class TeleApi:

    def __init__(self, item, link):
        self.item = item
        self.link = link

    def getFooBar(self, x, y):
        return x - y

    async def customshare(self):
        return await CustomShare.anit(self.link, 42)

class TeleAware(s_telepath.Aware):
    def __init__(self):
        s_telepath.Aware.__init__(self)
        self.beeps = {}

    def _initBeep(self, path):
        beep = self.beeps.get(path)
        if beep:
            return beep
        beep = Beep(path)
        self.beeps[path] = beep
        return beep

    def getTeleApi(self, link, mesg, path):
        if not path:
            return TeleApi(self, link)

        return self._initBeep(path[0])

class TeleAuth(s_telepath.Aware):

    def getTeleApi(self, link, mesg, path):

        auth = mesg[1].get('auth')
        if auth is None:
            raise s_exc.AuthDeny()

        user, info = auth

        passwd = info.get('passwd')
        if passwd != 'secretsauce':
            raise s_exc.AuthDeny()

        return self

    def getFooBar(self, x, y):
        return x + y

class TeleTest(s_t_utils.SynTest):

    async def test_telepath_basics(self):

        foo = Foo()
        evt = asyncio.Event()

        async with self.getTestDmon() as dmon:

            dmon.share('foo', foo)

            await self.asyncraises(s_exc.BadUrl, s_telepath.openurl('noscheme/foo'))

            prox = await s_telepath.openurl('tcp://127.0.0.1/foo', port=dmon.addr[1])

            # Some bookkeeping data about the connection is available
            # from the daemon related to the session's objects and
            # connection information.
            snfo = await dmon.getSessInfo()
            self.len(1, snfo)
            self.eq(snfo[0].get('items'), {None: 'synapse.tests.test_telepath.Foo'})
            conninfo = snfo[0].get('conninfo')
            self.isinstance(conninfo, dict)
            self.eq(conninfo.get('family'), 'tcp')
            self.eq(conninfo.get('ipver'), 'ipv4')
            # The prox's local sock.getsockname() corresponds to the
            # server's sock.getpeername()
            self.eq(conninfo.get('addr'), prox.link.sock.getsockname())

            # Prox exposes remote synapse version
            self.eq(prox._getSynVers(), s_version.version)

            # Prox exposes reflected classes
            self.eq(prox._getClasses(),
                    ('synapse.tests.test_telepath.Foo',))

            # Add an additional prox.fini handler.
            prox.onfini(evt.set)

            # check a standard return value
            self.eq(30, await prox.bar(10, 20))

            # check a coroutine return value
            self.eq(25, await prox.corovalu(10, 5))

            # check a generator return channel
            genr = await prox.genr()
            self.true(isinstance(genr, s_coro.GenrHelp))
            self.eq((10, 20, 30), await genr.list())

            # check generator explodes channel
            genr = await prox.genrboom()
            await self.asyncraises(s_exc.SynErr, genr.list())

            # check an async generator return channel
            genr = prox.corogenr(3)
            self.true(isinstance(genr, s_telepath.GenrIter))
            self.eq((0, 1, 2), await alist(genr))

            await self.asyncraises(s_exc.NoSuchMeth, prox.raze())

            await self.asyncraises(s_exc.NoSuchMeth, prox.fake())

            await self.asyncraises(s_exc.SynErr, prox.boom())

        # Fini'ing a daemon fini's proxies connected to it.
        self.true(await s_coro.event_wait(evt, 2))
        self.true(prox.isfini)
        await self.asyncraises(s_exc.IsFini, prox.bar((10, 20)))

    async def test_telepath_sync_genr(self):

        foo = Foo()

        def sync():
            return [x for x in prox.genr()]

        async with self.getTestDmon() as dmon:

            dmon.share('foo', foo)

            async with await s_telepath.openurl('tcp://127.0.0.1/foo', port=dmon.addr[1]) as prox:
                self.eq((10, 20, 30), await s_coro.executor(sync))

    def test_telepath_sync_genr_break(self):

        try:
            acm = self.getTestCoreAndProxy()
            core, proxy = s_glob.sync(acm.__aenter__())

            form = 'test:int'

            q = '[' + ' '.join([f'{form}={i}' for i in range(10)]) + ' ]'

            # This puts a link into the link pool
            podes = list(proxy.eval(q))
            self.len(10, podes)

            evt = threading.Event()

            # Get the link from the pool, add the fini callback and put it back
            link = s_glob.sync(proxy.getPoolLink())
            link.onfini(evt.set)
            s_glob.sync(proxy._putPoolLink(link))

            q = f'{form} | sleep 0.1'

            # Break from the generator right away, causing a
            # GeneratorExit in the GenrHelp object __iter__ method.
            pode = None
            for pode in proxy.eval(q):
                break
            # Ensure the query did yield an object
            self.nn(pode)

            # Ensure the link we have a reference too was torn down
            self.true(evt.wait(4))
            self.true(link.isfini)

        finally:
            s_glob.sync(acm.__aexit__(None, None, None))

    async def test_telepath_no_sess(self):

        foo = Foo()
        evt = asyncio.Event()

        async with self.getTestDmon() as dmon:

            dmon.share('foo', foo)

            await self.asyncraises(s_exc.BadUrl, s_telepath.openurl('noscheme/foo'))

            async with await s_telepath.openurl('tcp://127.0.0.1/foo', port=dmon.addr[1]) as prox:

                prox.sess = None

                # Add an additional prox.fini handler.
                prox.onfini(evt.set)

                # check a standard return value
                self.eq(30, await prox.bar(10, 20))

                # check a coroutine return value
                self.eq(25, await prox.corovalu(10, 5))

                # check a generator return channel
                genr = await prox.genr()
                self.eq((10, 20, 30), await alist(genr))

                # check an async generator return channel
                genr = prox.corogenr(3)
                self.eq((0, 1, 2), await alist(genr))

                await self.asyncraises(s_exc.NoSuchMeth, prox.raze())

                await self.asyncraises(s_exc.NoSuchMeth, prox.fake())

                await self.asyncraises(s_exc.SynErr, prox.boom())

            # Fini'ing a daemon fini's proxies connected to it.
            self.true(await s_coro.event_wait(evt, 2))
            self.true(prox.isfini)
            await self.asyncraises(s_exc.IsFini, prox.bar((10, 20)))

    async def test_telepath_tls_bad_cert(self):
        self.thisHostMustNot(platform='darwin')

        foo = Foo()

        async with self.getTestDmon() as dmon:
            # As a workaround to a Python bug (https://bugs.python.org/issue30945) that prevents localhost:0 from
            # being connected via TLS, make a certificate for whatever my hostname is and sign it with the test CA
            # key.
            hostname = socket.gethostname()
            dmon.certdir.genHostCert(socket.gethostname())

            addr = await dmon.listen(f'ssl://{hostname}:0')
            dmon.share('foo', foo)

            # host cert is *NOT* signed by a CA that client recognizes
            await self.asyncraises(ssl.SSLCertVerificationError,
                                   s_telepath.openurl(f'ssl://{hostname}/foo', port=addr[1]))

    async def test_telepath_tls(self):
        self.thisHostMustNot(platform='darwin')

        foo = Foo()

        async with self.getTestDmon() as dmon:
            # As a workaround to a Python bug (https://bugs.python.org/issue30945) that prevents localhost:0 from
            # being connected via TLS, make a certificate for whatever my hostname is and sign it with the test CA
            # key.
            hostname = socket.gethostname()

            dmon.certdir.genHostCert(hostname, signas='ca')

            addr = await dmon.listen(f'ssl://{hostname}:0')

            dmon.share('foo', foo)

            async with await s_telepath.openurl(f'ssl://{hostname}/foo', port=addr[1]) as prox:
                self.eq(30, await prox.bar(10, 20))

                # The daemon's session information for a TLS link
                # its own family.
                sessions = await dmon.getSessInfo()
                self.len(1, sessions)
                self.eq(sessions[0].get('conninfo').get('family'), 'tls')

    async def test_telepath_surrogate(self):

        foo = Foo()
        async with self.getTestDmon() as dmon:

            dmon.share('foo', foo)

            async with await s_telepath.openurl('tcp://127.0.0.1/foo', port=dmon.addr[1]) as prox:
                bads = '\u01cb\ufffd\ud842\ufffd\u0012'
                t0 = ('1234', {'key': bads})

                # Shovel a malformed UTF8 string with an unpaired surrogate over telepath
                ret = await prox.echo(t0)
                self.eq(ret, t0)

    async def test_telepath_async(self):

        foo = Foo()

        async with self.getTestDmon() as dmon:

            dmon.share('foo', foo)

            async with await s_telepath.openurl('tcp://127.0.0.1/foo', port=dmon.addr[1]) as prox:

                genr = prox.corogenr(3)
                self.eq([0, 1, 2], [x async for x in genr])
                # To act the same as a local object, would be:
                # self.eq([0, 1, 2], [x async for x in genr])

                aitr = prox.corogenr('fred').__aiter__()
                await self.asyncraises(s_exc.SynErr, aitr.__anext__())

                aitr = prox.corogenr(3).__aiter__()
                await aitr.__anext__()

                start_event = asyncio.Event()

                async def longwaiter():
                    coro = prox.longasync()
                    await start_event.wait()
                    await coro

                task = dmon.schedCoro(longwaiter())

            await self.asyncraises(StopAsyncIteration, aitr.__anext__())
            start_event.set()

            # Test that a coroutine about to await on an async proxy method doesn't become "stuck" by awaiting on a
            # just-fini'd object method

            # Give the longwaiter a chance to run
            await asyncio.sleep(.1)

            await self.asyncraises(s_exc.IsFini, asyncio.wait_for(task, timeout=2))

    async def test_telepath_blocking(self):
        ''' Make sure that async methods on the same proxy don't block each other '''

        class MyClass():
            typename = 'myshare'

            def __init__(self):
                self.evnt = asyncio.Event()
                self.sema = asyncio.Semaphore(value=0)

            async def do_it(self):
                self.sema.release()
                await self.evnt.wait()

            async def wait_for_doits(self):
                await self.sema.acquire()
                await self.sema.acquire()
                self.evnt.set()

        bar = MyClass()

        async with self.getTestDmon() as dmon:

            addr = await dmon.listen('tcp://127.0.0.1:0')

            dmon.share('bar', bar)

            prox = await s_telepath.openurl('tcp://127.0.0.1/bar', port=addr[1])

            # Check proxy objects, and also make sure that it doesn't block on server

            tasks = [prox.do_it() for _ in range(2)]
            tasks.append(prox.wait_for_doits())
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            await prox.fini()

    async def test_telepath_aware(self):

        item = TeleAware()

        async with self.getTestDmon() as dmon:
            dmon.share('woke', item)
            async with await self.getTestProxy(dmon, 'woke') as proxy:

                # Ensure the session tracks the reference to the TeleApi object
                sess = dmon.sessions[list(dmon.sessions.keys())[0]]
                self.isinstance(sess.getSessItem(None), TeleApi)
                # And that data is available from the session helper API
                snfo = await dmon.getSessInfo()
                self.len(1, snfo)
                self.eq(snfo[0].get('items'), {None: 'synapse.tests.test_telepath.TeleApi'})

                self.eq(10, await proxy.getFooBar(20, 10))

                # check a custom share works
                obj = await proxy.customshare()
                self.eq(999, await obj.boo(999))

                # Ensure the Share object is placed into the
                # session for the daemon.
                self.len(2, sess.items)
                key = [k for k in sess.items.keys() if k][0]
                self.isinstance(sess.getSessItem(key), CustomShare)

                # make another customshare reference which will be
                # tracked by the Sess object
                evt = asyncio.Event()
                async with await proxy.customshare() as _share:
                    self.len(3, sess.items)
                    _key = [k for k in sess.items.keys() if k and k != key][0]
                    _cshare = sess.getSessItem(_key)
                    self.isinstance(_cshare, CustomShare)
                    _cshare.onfini(evt.set)

                # and that item is removed from the sess on the
                # _share fini by the client
                self.true(await asyncio.wait_for(evt.wait(), 6))
                self.len(2, sess.items)
                self.nn(sess.getSessItem(key))

                # ensure that the share is represented in the session info via
                # the helper APIs as well
                snfo = await dmon.getSessInfo()
                self.len(1, snfo)
                self.eq(snfo[0].get('items'),
                        {None: 'synapse.tests.test_telepath.TeleApi',
                         key: 'synapse.tests.test_telepath.CustomShare'})

                # and we can still use the first obj we made
                ret = await alist(obj.custgenr(3))
                self.eq(ret, [0, 1, 2])

            # check that a dynamic share works
            async with await self.getTestProxy(dmon, 'woke/up') as proxy:
                self.eq('up: beep', await proxy.beep())

    async def test_telepath_auth(self):

        item = TeleAuth()
        async with self.getTestDmon() as dmon:
            dmon.share('auth', item)
            host, port = dmon.addr

            url = 'tcp://localhost/auth'
            await self.asyncraises(s_exc.AuthDeny, s_telepath.openurl(url, port=port))

            url = 'tcp://visi@localhost/auth'
            await self.asyncraises(s_exc.AuthDeny, s_telepath.openurl(url, port=port))

            url = 'tcp://visi:secretsauce@localhost/auth'
            async with await s_telepath.openurl(url, port=port) as proxy:
                self.eq(17, await proxy.getFooBar(10, 7))

    async def test_telepath_server_badvers(self):

        async with self.getTestDmon() as dmon:

            dmon.televers = (0, 0)

            host, port = await dmon.listen('tcp://127.0.0.1:0/')

            await self.asyncraises(s_exc.BadMesgVers, s_telepath.openurl('tcp://127.0.0.1/', port=port))

    async def test_alias(self):

        item = TeleAware()
        name = 'item'

        async with self.getTestDmon() as dmon:

            host, port = dmon.addr
            dmon.share(name, item)

            with self.getTestDir() as dirn:

                url = f'tcp://{host}:{port}/{name}'
                beepbeep_alias = url + '/beepbeep'
                aliases = {name: url,
                           f'{name}/borp': beepbeep_alias}

                with self.setSynDir(dirn):

                    fp = s_common.getSynPath('aliases.yaml')
                    s_common.yamlsave(aliases, fp)

                    # None existent aliases return None
                    self.none(s_telepath.alias('newp'))
                    self.none(s_telepath.alias('newp/path'))

                    # An exact match wins
                    self.eq(s_telepath.alias(name), url)
                    self.eq(s_telepath.alias(f'{name}/borp'), beepbeep_alias)
                    # Dynamic aliases are valid.
                    self.eq(s_telepath.alias(f'{name}/beepbeep'), beepbeep_alias)

                    async with await s_telepath.openurl(name) as prox:
                        self.eq(10, await prox.getFooBar(20, 10))

                    # Check to see that we can connect to an aliased name
                    # with a dynamic share attached to it.
                    async with await s_telepath.openurl(f'{name}/bar') as prox:
                        self.eq('bar: beep', await prox.beep())

    async def test_default_name(self):

        async with self.getTestDmon() as dmon:

            host, port = dmon.addr
            dmon.share('*', Foo())

            async with await s_telepath.openurl(f'tcp://{host}:{port}/') as prox:
                self.eq('hiya', await prox.echo('hiya'))

    async def test_url_cell(self):

        with self.getTestDir(chdir=True) as dirn:

            path = os.path.join(dirn, 'cell')
            sockpath = os.path.join(path, 'sock')

            async with await s_cell.Cell.anit(path) as cell:

                # test a relative cell:// url
                async with await s_telepath.openurl('cell://cell') as prox:
                    self.eq('cell', await prox.getCellType())
                    # unix path information is available from the session information.
                    snfo = await cell.dmon.getSessInfo()
                    self.eq(snfo[0].get('conninfo'),
                            {'family': 'unix',
                             'addr': sockpath})

                # test an absolute cell:// url
                async with await s_telepath.openurl(f'cell://{path}') as prox:
                    self.eq('cell', await prox.getCellType())
                    # unix path information is available from the session information.
                    snfo = await cell.dmon.getSessInfo()
                    self.eq(snfo[0].get('conninfo'),
                            {'family': 'unix',
                             'addr': sockpath})

    async def test_ipv6(self):

        foo = Foo()

        async with self.getTestDmon() as dmon:

            dmon.share('foo', foo)
            try:
                addr = await dmon.listen('tcp://[::1]:0/')
            except asyncio.CancelledError:
                raise
            except OSError:
                if os.getenv('CIRCLECI', False):
                    # Circleci container tests do not support IPV6 (but osx does)
                    # https://circleci.com/docs/2.0/faq/#can-i-use-ipv6-in-my-tests
                    self.skip('ipv6 is not supported in circleci')
                else:
                    raise
            host, port = addr[0], addr[1]

            async with await s_telepath.openurl(f'tcp://{host}/foo',
                                                port=port) as prox:
                # Ensure that ipv6 is returned via session info
                snfo = await dmon.getSessInfo()
                conninfo = snfo[0].get('conninfo')
                self.eq(conninfo, {'family': 'tcp',
                               'ipver': 'ipv6',
                               'addr': prox.link.sock.getsockname()})

                # check a standard return value
                self.eq(30, await prox.bar(10, 20))

    async def test_telepath_client_redir(self):

        class TestRedir(s_telepath.Aware):

            def __init__(self, valu, redir=None):
                self.valu = valu
                self.redir = redir

            def getTeleApi(self, link, mesg, path):
                if self.redir is not None:
                    raise s_exc.TeleRedir(url=self.redir)
                return self

            async def dostuff(self, x):

                if self.redir:
                    raise s_exc.TeleRedir(url=self.redir)

                return x + self.valu

        dmon0 = await s_daemon.Daemon.anit()
        dmon1 = await s_daemon.Daemon.anit()

        addr0 = await dmon0.listen('tcp://127.0.0.1:0/')
        addr1 = await dmon1.listen('tcp://127.0.0.1:0/')

        url0 = f'tcp://127.0.0.1:{addr0[1]}/foo'
        url1 = f'tcp://127.0.0.1:{addr1[1]}/foo'

        rdir0 = TestRedir(10)
        rdir1 = TestRedir(20, redir=url0)

        dmon0.share('foo', rdir0)
        dmon1.share('foo', rdir1)

        async with await s_telepath.Client.anit(url0) as targ:
            await targ.waitready()
            # Client implements some base helpers the proxy does
            self.eq(targ._getSynVers(), s_version.version)
            self.eq(targ._getClasses(),
                    ('synapse.tests.test_telepath.TestRedir',
                     'synapse.telepath.Aware'))
            # client works as a passthrough to the proxy
            self.eq(110, await targ.dostuff(100))

        # this should get redirected to url0...
        async with await s_telepath.Client.anit(url1) as targ:
            await targ.waitready()
            self.eq(110, await targ.dostuff(100))

        # fake out the redirect to connect, then redirect on call...
        rdir1.redir = None
        async with await s_telepath.Client.anit(url1) as targ:
            await targ.waitready()
            self.eq(120, await targ.dostuff(100))
            rdir1.redir = url0
            self.eq(110, await targ.dostuff(100))

        await dmon0.fini()
        await dmon1.fini()

    async def test_telepath_client_failover(self):

        class TestFail:
            def __init__(self):
                self.count = 0

            async def dostuff(self, x):
                self.count += 1
                return x + 10

        dmon0 = await s_daemon.Daemon.anit()
        dmon1 = await s_daemon.Daemon.anit()

        addr0 = await dmon0.listen('tcp://127.0.0.1:0/')
        addr1 = await dmon1.listen('tcp://127.0.0.1:0/')

        url0 = f'tcp://127.0.0.1:{addr0[1]}/foo'
        url1 = f'tcp://127.0.0.1:{addr1[1]}/foo'

        fail0 = TestFail()
        fail1 = TestFail()

        dmon0.share('foo', fail0)
        dmon1.share('foo', fail1)

        urls = (url0, url1)

        async with await s_telepath.Client.anit(urls) as targ:

            await targ.waitready()

            self.eq(110, await targ.dostuff(100))
            self.eq(1, fail0.count)
            self.eq(0, fail1.count)

            await dmon0.fini()

            self.eq(110, await targ.dostuff(100))
            self.eq(1, fail0.count)
            self.eq(1, fail1.count)

        async with await s_telepath.Client.anit(urls) as targ:

            await targ.waitready()

            self.eq(110, await targ.dostuff(100))

            self.eq(1, fail0.count)
            self.eq(2, fail1.count)

        await dmon1.fini()

    async def test_telepath_poolsize(self):

        # While test_telepath_sync_genr_break also touches the link pool,
        # it doesn't validate the pool size or automatic link teardown
        # behavior when a extra link is placed into the pool.
        foo = Foo()

        async with self.getTestDmon() as dmon:
            dmon.share('foo', foo)
            url = f'tcp://127.0.0.1:{dmon.addr[1]}/foo'

            # Validate the Proxy behavior then the client override
            prox = await s_telepath.openurl(url)  # type: Foo
            prox._link_poolsize = 2

            # Start with no links
            self.len(0, prox.links)
            self.eq(await prox.echo(1), 1)

            # We now have one link - spin up a generator to grab it
            self.len(1, prox.links)
            l0 = prox.links[0]
            genr = await prox.genr()  # type: s_coro.GenrHelp
            self.eq(await genr.genr.__anext__(), 10)

            # The link is being used by the genr
            self.len(0, prox.links)

            # and upon exhuastion, that link is put back
            self.eq(await genr.list(), (20, 30))
            self.len(1, prox.links)
            self.true(prox.links[0] is l0)

            # Grab the existing link, then do two more calls
            genr0 = await prox.genr()  # contains l0
            genr1 = await prox.genr()
            genr2 = await prox.genr()
            self.len(0, prox.links)
            # Consume two of the three generators
            self.eq(await genr2.list(), (10, 20, 30))
            self.len(1, prox.links)
            self.eq(await genr1.list(), (10, 20, 30))
            self.len(2, prox.links)
            # Exhausting the lsat generator results in his
            # link not being placed back into the pool
            self.eq(await genr0.list(), (10, 20, 30))
            self.len(2, prox.links)
            links = set(l for l in prox.links)
            self.notin(l0, links)
            # And that link l0 has been fini'd
            self.true(l0.isfini)

            # Tear down a link by hand and place it back
            # into the pool - that will fail b/c the link
            # has been down down.
            l1 = await prox.getPoolLink()
            self.len(1, prox.links)
            await l1.fini()
            await prox._putPoolLink(l1)
            self.len(1, prox.links)

            # And all our links are torn down on fini
            await prox.fini()
            self.len(1, prox.links)
            for link in prox.links:
                self.true(link.isfini)

            # The telepath Client passes through this value as a configuration parameter
            conf = {'link_poolsize': 2, 'timeout': 2}
            async with await s_telepath.Client.anit(url, conf=conf) as client:
                await client.waitready()
                self.true(client._t_proxy._link_poolsize, 2)

    async def test_link_fini_breaking_tasks(self):
        foo = Foo()

        async with self.getTestDmon() as dmon:
            dmon.share('foo', foo)
            url = f'tcp://127.0.0.1:{dmon.addr[1]}/foo'

            prox = await s_telepath.openurl(url)  # type: Foo

            # Fire up an async generator which will yield a message then
            # wait for a while
            async for mesg in prox.sleepg(t=60):
                self.eq(mesg, ('init', {}))
                break

            # Ensure that tearing down the client prompty tears down
            # taskv2init coro due to the link being fini'd by the server.
            # It is important that we validate these items BEFORE we
            # teardown the proxy, since the previous (<0.1.32) behaviour
            # would hold onto coroutines on the Daemon and not cancel
            # the taskv2init coroutines until the Daemon was shut down.
            with self.getAsyncLoggerStream('synapse.daemon',
                                           'task sleepg') as stream:
                await prox.fini()
                # Ensure that the sleepg function got canceled.
                await asyncio.sleep(1)
                self.true(await asyncio.wait_for(foo.sleepg_evt.wait(), timeout=6))
                # Ensure we logged the cancellation.
                self.true(await stream.wait(6))
