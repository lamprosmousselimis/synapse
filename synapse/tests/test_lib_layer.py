import math
import asyncio
import contextlib

import synapse.exc as s_exc
import synapse.common as s_common
import synapse.telepath as s_telepath

import synapse.lib.layer as s_layer
import synapse.lib.msgpack as s_msgpack

import synapse.tests.utils as s_t_utils

from synapse.tests.utils import alist

async def iterPropForm(self, form=None, prop=None):
    bad_valu = [(b'foo', "bar"), (b'bar', ('bar',)), (b'biz', 4965), (b'baz', (0, 56))]
    bad_valu += [(b'boz', 'boz')] * 10
    for buid, valu in bad_valu:
        yield buid, valu

@contextlib.contextmanager
def patch_snap(snap):
    old_layr = []
    for layr in snap.layers:
        old_layr.append((layr.iterPropRows, layr.iterUnivRows))
        layr.iterPropRows, layr.iterUnivRows = (iterPropForm,) * 2

    yield

    for layr_idx, layr in enumerate(snap.layers):
        layr.iterPropRows, layr.iterUnivRows = old_layr[layr_idx]

class LayerTest(s_t_utils.SynTest):

    async def test_layer_abrv(self):

        async with self.getTestCore() as core:

            layr = core.getLayer()
            self.eq(b'\x00\x00\x00\x00\x00\x00\x00\x04', layr.getPropAbrv('visi', 'foo'))
            # another to check the cache...
            self.eq(b'\x00\x00\x00\x00\x00\x00\x00\x04', layr.getPropAbrv('visi', 'foo'))
            self.eq(b'\x00\x00\x00\x00\x00\x00\x00\x05', layr.getPropAbrv('whip', None))
            self.eq(('visi', 'foo'), layr.getAbrvProp(b'\x00\x00\x00\x00\x00\x00\x00\x04'))
            self.eq(('whip', None), layr.getAbrvProp(b'\x00\x00\x00\x00\x00\x00\x00\x05'))
            self.eq(None, layr.getAbrvProp(b'\x00\x00\x00\x00\x00\x00\x00\x06'))

            self.eq(b'\x00\x00\x00\x00\x00\x00\x00\x00', layr.getTagPropAbrv('visi', 'foo'))
            # another to check the cache...
            self.eq(b'\x00\x00\x00\x00\x00\x00\x00\x00', layr.getTagPropAbrv('visi', 'foo'))
            self.eq(b'\x00\x00\x00\x00\x00\x00\x00\x01', layr.getTagPropAbrv('whip', None))

    async def test_layer_upstream(self):

        with self.getTestDir() as dirn:

            path00 = s_common.gendir(dirn, 'core00')
            path01 = s_common.gendir(dirn, 'core01')

            async with self.getTestCore(dirn=path00) as core00:

                layriden = core00.view.layers[0].iden

                await core00.nodes('[test:str=foobar +#hehe.haha]')
                await core00.nodes('[ inet:ipv4=1.2.3.4 ]')
                await core00.addTagProp('score', ('int', {}), {})

                async with await core00.snap() as snap:

                    props = {'tick': 12345}
                    node = await snap.addNode('test:str', 'foo', props=props)
                    await node.setTagProp('bar', 'score', 10)
                    await node.setData('baz', 'nodedataiscool')

                    node = await snap.addNode('test:str', 'bar', props=props)
                    await node.setData('baz', 'nodedataiscool')

                async with self.getTestCore(dirn=path01) as core01:

                    # test layer/<iden> mapping
                    async with core00.getLocalProxy(f'*/layer/{layriden}') as layrprox:
                        self.eq(layriden, await layrprox.getIden())

                    url = core00.getLocalUrl('*/layer')
                    conf = {'upstream': url}
                    ldef = await core01.addLayer(ldef=conf)
                    layr = core01.getLayer(ldef.get('iden'))
                    await core01.view.addLayer(layr.iden)

                    # test initial sync
                    offs = core00.getView().layers[0].getNodeEditOffset()
                    evnt = await layr.waitUpstreamOffs(layriden, offs)
                    await asyncio.wait_for(evnt.wait(), timeout=2.0)

                    self.len(1, await core01.nodes('inet:ipv4=1.2.3.4'))
                    nodes = await core01.nodes('test:str=foobar')
                    self.len(1, nodes)
                    self.nn(nodes[0].tags.get('hehe.haha'))

                    async with await core01.snap() as snap:
                        node = await snap.getNodeByNdef(('test:str', 'foo'))
                        self.nn(node)
                        self.eq(node.props.get('tick'), 12345)
                        self.eq(node.tagprops.get(('bar', 'score')), 10)
                        self.eq(await node.getData('baz'), 'nodedataiscool')

                    # make sure updates show up
                    await core00.nodes('[ inet:fqdn=vertex.link ]')

                    offs = core00.getView().layers[0].getNodeEditOffset()
                    evnt = await layr.waitUpstreamOffs(layriden, offs)
                    await asyncio.wait_for(evnt.wait(), timeout=2.0)

                    self.len(1, await core01.nodes('inet:fqdn=vertex.link'))

                await core00.nodes('[ inet:ipv4=5.5.5.5 ]')
                offs = core00.getView().layers[0].getNodeEditOffset()

                # test what happens when we go down and come up again...
                async with self.getTestCore(dirn=path01) as core01:

                    layr = core01.getView().layers[-1]

                    evnt = await layr.waitUpstreamOffs(layriden, offs)
                    await asyncio.wait_for(evnt.wait(), timeout=2.0)

                    self.len(1, await core01.nodes('inet:ipv4=5.5.5.5'))

                    await core00.nodes('[ inet:ipv4=5.6.7.8 ]')

                    offs = core00.getView().layers[0].getNodeEditOffset()
                    evnt = await layr.waitUpstreamOffs(layriden, offs)
                    await asyncio.wait_for(evnt.wait(), timeout=2.0)

                    self.len(1, await core01.nodes('inet:ipv4=5.6.7.8'))

                    # make sure time and user are set on the downstream splices
                    root = await core01.auth.getUserByName('root')

                    splices = await alist(layr.splicesBack(size=1))
                    self.len(1, splices)

                    splice = splices[0][1][1]
                    self.nn(splice.get('time'))
                    self.eq(splice.get('user'), root.iden)
                    self.none(splice.get('prov'))

    async def test_layer_multi_upstream(self):

        with self.getTestDir() as dirn:

            path00 = s_common.gendir(dirn, 'core00')
            path01 = s_common.gendir(dirn, 'core01')
            path02 = s_common.gendir(dirn, 'core02')

            async with self.getTestCore(dirn=path00) as core00:

                iden00 = core00.view.layers[0].iden

                await core00.nodes('[test:str=foobar +#hehe.haha]')
                await core00.nodes('[ inet:ipv4=1.2.3.4 ]')

                async with self.getTestCore(dirn=path01) as core01:

                    iden01 = core01.view.layers[0].iden

                    await core01.nodes('[test:str=barfoo +#haha.hehe]')
                    await core01.nodes('[ inet:ipv4=4.3.2.1 ]')

                    async with self.getTestCore(dirn=path02) as core02:

                        url00 = core00.getLocalUrl('*/layer')
                        url01 = core01.getLocalUrl('*/layer')

                        conf = {'upstream': [url00, url01]}

                        ldef = await core02.addLayer(ldef=conf)
                        layr = core02.getLayer(ldef.get('iden'))
                        await core02.view.addLayer(layr.iden)

                        # core00 is synced
                        offs = core00.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden00, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:ipv4=1.2.3.4'))
                        nodes = await core02.nodes('test:str=foobar')
                        self.len(1, nodes)
                        self.nn(nodes[0].tags.get('hehe.haha'))

                        # core01 is synced
                        offs = core01.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden01, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:ipv4=4.3.2.1'))
                        nodes = await core02.nodes('test:str=barfoo')
                        self.len(1, nodes)
                        self.nn(nodes[0].tags.get('haha.hehe'))

                        # updates from core00 show up
                        await core00.nodes('[ inet:fqdn=vertex.link ]')

                        offs = core00.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden00, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:fqdn=vertex.link'))

                        # updates from core01 show up
                        await core01.nodes('[ inet:fqdn=google.com ]')

                        offs = core01.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden01, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:fqdn=google.com'))

                    await core00.nodes('[ inet:ipv4=5.5.5.5 ]')
                    await core01.nodes('[ inet:ipv4=6.6.6.6 ]')

                    # test what happens when we go down and come up again...
                    async with self.getTestCore(dirn=path02) as core02:

                        layr = core02.getView().layers[-1]

                        # test we catch up to core00
                        offs = core00.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden00, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:ipv4=5.5.5.5'))

                        # test we catch up to core01
                        offs = core01.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden01, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:ipv4=6.6.6.6'))

                        # test we get updates from core00
                        await core00.nodes('[ inet:ipv4=5.6.7.8 ]')

                        offs = core00.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden00, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:ipv4=5.6.7.8'))

                        # test we get updates from core01
                        await core01.nodes('[ inet:ipv4=8.7.6.5 ]')

                        offs = core01.getView().layers[0].getNodeEditOffset()
                        evnt = await layr.waitUpstreamOffs(iden01, offs)
                        await asyncio.wait_for(evnt.wait(), timeout=2.0)

                        self.len(1, await core02.nodes('inet:ipv4=8.7.6.5'))

    async def test_layer_splices(self):

        async with self.getTestCore() as core:

            layr = core.view.layers[0]
            root = await core.auth.getUserByName('root')

            splices = await alist(layr.splices(None, 10))
            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            await core.addTagProp('risk', ('int', {'minval': 0, 'maxval': 100}), {'doc': 'risk score'})

            # Convert a node:add splice
            await core.nodes('[ test:str=foo ]')

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'node:add')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a prop:set splice with no oldv
            await core.nodes("test:str=foo [ :tick=2000 ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'prop:set')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['prop'], 'tick')
            self.eq(splice[1]['valu'], 946684800000)
            self.eq(splice[1]['oldv'], None)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a prop:set splice with an oldv
            await core.nodes("test:str=foo [ :tick=2001 ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'prop:set')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['prop'], 'tick')
            self.eq(splice[1]['valu'], 978307200000)
            self.eq(splice[1]['oldv'], 946684800000)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a prop:del splice
            await core.nodes("test:str=foo [ -:tick ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'prop:del')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['prop'], 'tick')
            self.eq(splice[1]['valu'], 978307200000)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a tag:add splice with no oldv
            await core.nodes("test:str=foo [ +#haha=2000 ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[4][1]
            self.eq(splice[0], 'tag:add')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['tag'], 'haha')
            self.eq(splice[1]['valu'], (946684800000, 946684800001))
            self.eq(splice[1]['oldv'], None)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a tag:add splice with an oldv
            await core.nodes("test:str=foo [ +#haha=2001 ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'tag:add')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['tag'], 'haha')
            self.eq(splice[1]['valu'], (946684800000, 978307200001))
            self.eq(splice[1]['oldv'], (946684800000, 946684800001))
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a tag:del splice
            await core.nodes("test:str=foo [ -#haha ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'tag:del')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['tag'], 'haha')
            self.eq(splice[1]['valu'], (946684800000, 978307200001))
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a tag:prop:add splice with no oldv
            await core.nodes("test:str=foo [ +#rep:risk=50 ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[5][1]
            self.eq(splice[0], 'tag:prop:set')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['tag'], 'rep')
            self.eq(splice[1]['prop'], 'risk')
            self.eq(splice[1]['valu'], 50)
            self.eq(splice[1]['oldv'], None)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a tag:prop:add splice with an oldv
            await core.nodes("test:str=foo [ +#rep:risk=0 ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'tag:prop:set')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['tag'], 'rep')
            self.eq(splice[1]['prop'], 'risk')
            self.eq(splice[1]['valu'], 0)
            self.eq(splice[1]['oldv'], 50)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Convert a tag:prop:del splice
            await core.nodes("test:str=foo [ -#rep:risk ]")

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[0][1]
            self.eq(splice[0], 'tag:prop:del')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['tag'], 'rep')
            self.eq(splice[1]['prop'], 'risk')
            self.eq(splice[1]['valu'], 0)
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            spliceoffs = (splices[-1][0][0] + 1, 0, 0)

            # Nodedata edits don't make splices
            nodes = await core.nodes('test:str=foo')
            await nodes[0].setData('baz', 'nodedataiscool')

            splices = await alist(layr.splices(spliceoffs, 10))
            self.len(0, splices)

            # Convert a node:del splice
            await core.nodes('test:str=foo | delnode')

            splices = await alist(layr.splices(spliceoffs, 10))

            splice = splices[2][1]
            self.eq(splice[0], 'node:del')
            self.eq(splice[1]['ndef'], ('test:str', 'foo'))
            self.eq(splice[1]['user'], root.iden)
            self.nn(splice[1].get('time'))

            # Get all the splices
            await self.agenlen(26, layr.splices())

            # Get all but the first splice
            await self.agenlen(25, layr.splices((0, 0, 1)))

            await self.agenlen(4, layr.splicesBack((1, 0, 0)))

            # Make sure we still get two splices when
            # offset is not at the beginning of a nodeedit
            await self.agenlen(2, layr.splices((1, 0, 200), 2))
            await self.agenlen(2, layr.splicesBack((3, 0, -1), 2))

            # Use the layer api to get the splices
            url = core.getLocalUrl('*/layer')
            async with await s_telepath.openurl(url) as layrprox:
                await self.agenlen(26, layrprox.splices())

    async def test_layer_stortype_float(self):
        async with self.getTestCore() as core:

            layr = core.view.layers[0]
            tmpdb = layr.layrslab.initdb('temp', dupsort=True)

            stor = s_layer.StorTypeFloat(s_layer.STOR_TYPE_FLOAT64, 8)
            vals = [math.nan, -math.inf, -99999.9, -0.0000000001, -42.1, -0.0, 0.0, 0.000001, 42.1, 99999.9, math.inf]

            indxby = s_layer.IndxBy(layr, b'', tmpdb)
            self.raises(s_exc.NoSuchImpl, indxby.getNodeValu, s_common.guid())

            for key, val in ((stor.indx(v), s_msgpack.en(v)) for v in vals):
                layr.layrslab.put(key[0], val, db=tmpdb)

            # = -99999.9
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '=', -99999.9)]
            self.eq(retn, [-99999.9])

            # <= -99999.9
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '<=', -99999.9)]
            self.eq(retn, [-math.inf, -99999.9])

            # < -99999.9
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '<', -99999.9)]
            self.eq(retn, [-math.inf])

            # > 99999.9
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '>', 99999.9)]
            self.eq(retn, [math.inf])

            # >= 99999.9
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '>=', 99999.9)]
            self.eq(retn, [99999.9, math.inf])

            # <= 0.0
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '<=', 0.0)]
            self.eq(retn, [-math.inf, -99999.9, -42.1, -0.0000000001, -0.0, 0.0])

            # >= -0.0
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '>=', -0.0)]
            self.eq(retn, [-0.0, 0.0, 0.000001, 42.1, 99999.9, math.inf])

            # >= -42.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '>=', -42.1)]
            self.eq(retn, [-42.1, -0.0000000001, -0.0, 0.0, 0.000001, 42.1, 99999.9, math.inf])

            # > -42.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '>', -42.1)]
            self.eq(retn, [-0.0000000001, -0.0, 0.0, 0.000001, 42.1, 99999.9, math.inf])

            # < 42.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '<', 42.1)]
            self.eq(retn, [-math.inf, -99999.9, -42.1, -0.0000000001, -0.0, 0.0, 0.000001])

            # <= 42.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, '<=', 42.1)]
            self.eq(retn, [-math.inf, -99999.9, -42.1, -0.0000000001, -0.0, 0.0, 0.000001, 42.1])

            # -42.1 to 42.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, 'range=', (-42.1, 42.1))]
            self.eq(retn, [-42.1, -0.0000000001, -0.0, 0.0, 0.000001, 42.1])

            # 1 to 42.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, 'range=', (1.0, 42.1))]
            self.eq(retn, [42.1])

            # -99999.9 to -0.1
            retn = [s_msgpack.un(valu) for valu in stor.indxBy(indxby, 'range=', (-99999.9, -0.1))]
            self.eq(retn, [-99999.9, -42.1])

            # <= NaN
            self.genraises(s_exc.NotANumberCompared, stor.indxBy, indxby, '<=', math.nan)

            # >= NaN
            self.genraises(s_exc.NotANumberCompared, stor.indxBy, indxby, '>=', math.nan)

            # 1.0 to NaN
            self.genraises(s_exc.NotANumberCompared, stor.indxBy, indxby, 'range=', (1.0, math.nan))

    async def test_layer_stortype_merge(self):

        async with self.getTestCore() as core:

            layr = core.getLayer()
            nodes = await core.nodes('[ inet:ipv4=1.2.3.4 .seen=(2012,2014) +#foo.bar=(2012, 2014) ]')

            buid = nodes[0].buid
            ival = nodes[0].get('.seen')
            tick = nodes[0].get('.created')
            tagv = nodes[0].getTag('foo.bar')

            newival = (ival[0] + 100, ival[1] - 100)
            newtagv = (tagv[0] + 100, tagv[1] - 100)

            nodeedits = [
                (buid, 'inet:ipv4', (
                    (s_layer.EDIT_PROP_SET, ('.seen', newival, ival, s_layer.STOR_TYPE_IVAL)),
                )),
            ]

            await layr.storNodeEdits(nodeedits, {})

            self.len(1, await core.nodes('inet:ipv4=1.2.3.4 +.seen=(2012,2014)'))

            nodeedits = [
                (buid, 'inet:ipv4', (
                    (s_layer.EDIT_PROP_SET, ('.created', tick + 200, tick, s_layer.STOR_TYPE_MINTIME)),
                )),
            ]

            await layr.storNodeEdits(nodeedits, {})

            nodes = await core.nodes('inet:ipv4=1.2.3.4')
            self.eq(tick, nodes[0].get('.created'))

            nodeedits = [
                (buid, 'inet:ipv4', (
                    (s_layer.EDIT_PROP_SET, ('.created', tick - 200, tick, s_layer.STOR_TYPE_MINTIME)),
                )),
            ]

            await layr.storNodeEdits(nodeedits, {})

            nodes = await core.nodes('inet:ipv4=1.2.3.4')
            self.eq(tick - 200, nodes[0].get('.created'))

            nodes = await core.nodes('[ inet:ipv4=1.2.3.4 ]')
            self.eq(tick - 200, nodes[0].get('.created'))

            nodeedits = [
                (buid, 'inet:ipv4', (
                    (s_layer.EDIT_TAG_SET, ('foo.bar', newtagv, tagv)),
                )),
            ]

            await layr.storNodeEdits(nodeedits, {})

            nodes = await core.nodes('inet:ipv4=1.2.3.4')
            self.eq(tagv, nodes[0].getTag('foo.bar'))

            nodes = await core.nodes('inet:ipv4=1.2.3.4 [ +#foo.bar=2015 ]')
            self.eq((1325376000000, 1420070400001), nodes[0].getTag('foo.bar'))

    async def test_layer_nodeedits(self):

        async with self.getTestCoreAndProxy() as (core0, prox0):

            nodelist0 = []
            nodes = await core0.nodes('[ test:str=foo ]')
            nodelist0.extend(nodes)
            nodes = await core0.nodes('[ inet:ipv4=1.2.3.4 .seen=(2012,2014) +#foo.bar=(2012, 2014) ]')
            nodelist0.extend(nodes)

            nodelist0 = [node.pack() for node in nodelist0]

            count = 0
            editlist = []

            layr = core0.getLayer(None)
            necount = layr.nodeeditlog.index()
            async for _, nodeedits in prox0.syncLayerNodeEdits(0):
                editlist.append(nodeedits)
                count += 1
                if count == necount:
                    break

            async with self.getTestCore() as core1:

                url = core1.getLocalUrl('*/layer')

                async with await s_telepath.openurl(url) as layrprox:

                    for nodeedits in editlist:
                        self.nn(await layrprox.storNodeEdits(nodeedits))

                    nodelist1 = []
                    nodelist1.extend(await core1.nodes('test:str'))
                    nodelist1.extend(await core1.nodes('inet:ipv4'))

                    nodelist1 = [node.pack() for node in nodelist1]
                    self.eq(nodelist0, nodelist1)

                layr = core1.view.layers[0]

                # Empty the layer to try again

                await layr.truncate()

                async with await s_telepath.openurl(url) as layrprox:

                    for nodeedits in editlist:
                        self.none(await layrprox.storNodeEditsNoLift(nodeedits))

                    nodelist1 = []
                    nodelist1.extend(await core1.nodes('test:str'))
                    nodelist1.extend(await core1.nodes('inet:ipv4'))

                    nodelist1 = [node.pack() for node in nodelist1]
                    self.eq(nodelist0, nodelist1)

                    meta = {'user': s_common.guid(),
                            'time': 0,
                            }

                    await layr.truncate()

                    for nodeedits in editlist:
                        self.none(await layrprox.storNodeEditsNoLift(nodeedits, meta=meta))

                    lastoffs = layr.nodeeditlog.index()
                    for nodeedit in layr.nodeeditlog.sliceBack(lastoffs, 2):
                        self.eq(meta, nodeedit[1][1])

    async def test_layer(self):

        async with self.getTestCore() as core:

            await core.addTagProp('score', ('int', {}), {})

            layr = core.getLayer()
            self.eq(str(layr), f'Layer (Layer): {layr.iden}')

            nodes = await core.nodes('[test:str=foo .seen=(2015, 2016)]')
            buid = nodes[0].buid

            self.eq('foo', layr.getNodeValu(buid))
            self.eq((1420070400000, 1451606400000), layr.getNodeValu(buid, '.seen'))
            self.none(layr.getNodeValu(buid, 'noprop'))
            self.none(layr.getNodeTag(buid, 'faketag'))

            self.false(await layr.hasTagProp('score'))
            nodes = await core.nodes('[test:str=bar +#test:score=100]')

    async def test_layer_no_extra_logging(self):

        async with self.getTestCore() as core:
            '''
            For a do-nothing write, don't write new log entries
            '''
            await core.nodes('[test:str=foo .seen=(2015, 2016)]')
            layr = core.getLayer(None)
            lbefore = len(await alist(layr.splices()))
            await core.nodes('[test:str=foo .seen=(2015, 2016)]')
            lafter = len(await alist(layr.splices()))
            self.eq(lbefore, lafter)
