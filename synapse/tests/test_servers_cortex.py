import synapse.exc as s_exc
import synapse.common as s_common
import synapse.cortex as s_cortex
import synapse.telepath as s_telepath

import synapse.tools.backup as s_tools_backup

import synapse.tests.utils as s_t_utils

class CortexServerTest(s_t_utils.SynTest):

    async def test_server(self):

        with self.getTestDir() as dirn, self.withSetLoggingMock() as mock:
            outp = self.getTestOutp()
            guid = s_common.guid()

            argv = [dirn,
                    '--telepath', 'tcp://127.0.0.1:0/',
                    '--https', '0',
                    '--name', 'telecore']
            async with await s_cortex.Cortex.initFromArgv(argv, outp=outp) as core:

                async with await s_telepath.openurl(f'cell://{dirn}') as proxy:
                    # Make a node with the cortex
                    podes = await s_t_utils.alist(proxy.eval(f'[ou:org={guid}]'))
                    self.len(1, podes)

                self.true(core.dmon.shared.get('*') is core)
                self.true(core.dmon.shared.get('cortex') is core)
                self.true(core.dmon.shared.get('telecore') is core)

            # And data persists...
            async with await s_cortex.Cortex.initFromArgv(argv, outp=outp) as core:
                async with await s_telepath.openurl(f'cell://{dirn}') as proxy:
                    podes = await s_t_utils.alist(proxy.eval(f'ou:org={guid}'))
                    self.len(1, podes)

            self.eq(2, mock.call_count)

    async def test_server_mirror(self):

        with self.getTestDir() as dirn, self.withSetLoggingMock():

            path00 = s_common.gendir(dirn, 'core00')
            path01 = s_common.gendir(dirn, 'core01')

            async with self.getTestCore(dirn=path00) as core00:
                await core00.nodes('[ inet:ipv4=1.2.3.4 ]')

            s_tools_backup.backup(path00, path01)

            async with self.getTestCore(dirn=path00) as core00:

                outp = self.getTestOutp()
                argv = ['--telepath', 'tcp://127.0.0.1:0/',
                        '--https', '0',
                        '--mirror', core00.getLocalUrl(),
                        path01]

                # add a node for core01 to sync before window
                await core00.nodes('[ inet:ipv4=5.5.5.5 ]')
                offs = core00.nexsroot.getOffset()

                s_common.yamlsave({'logchanges': True}, path01, 'cell.yaml')

                async with await s_cortex.Cortex.initFromArgv(argv, outp=outp) as core01:

                    # TODO functionalize this API on the cortex (cell?)
                    await core01.nexsroot.waitForOffset(offs - 1)

                    self.len(1, await core01.nodes('inet:ipv4=5.5.5.5'))

                    # add a node for core01 to sync via window
                    self.len(1, await core00.nodes('[ inet:ipv4=6.6.6.6 ]'))

                    offs = core00.nexsroot.getOffset()
                    await core01.nexsroot.waitForOffset(offs - 1)

                    self.len(1, await core01.nodes('inet:ipv4=6.6.6.6'))

    async def test_server_mirror_badiden(self):

        with self.getTestDir() as dirn, self.withSetLoggingMock():

            path00 = s_common.gendir(dirn, 'core00')
            path01 = s_common.gendir(dirn, 'core01')

            argv0 = ['--telepath', 'tcp://127.0.0.1:0/',
                     '--https', '0',
                     '--name', 'srccore',
                     path00,
                     ]
            out0 = self.getTestOutp()
            async with await s_cortex.Cortex.initFromArgv(argv0, outp=out0) as core00:

                out1 = self.getTestOutp()
                argv1 = ['--telepath', 'tcp://127.0.0.1:0/',
                         '--https', '0',
                         '--mirror', core00.getLocalUrl(),
                         path01,
                         ]

                s_common.yamlsave({'logchanges': True}, path01, 'cell.yaml')

                async with await s_cortex.Cortex.initFromArgv(argv1, outp=out1) as core01:
                    await core01.waitfini(6)
                    self.true(core01.isfini)
