'''
Migrate Synapse from 0.1.x to 0.2.x.
'''
import os
import sys
import shutil
import asyncio
import logging
import argparse
import collections

import synapse.cortex as s_cortex
import synapse.common as s_common
import synapse.datamodel as s_datamodel

import synapse.lib.base as s_base
import synapse.lib.hive as s_hive
import synapse.lib.time as s_time
import synapse.lib.cache as s_cache
import synapse.lib.const as s_const
import synapse.lib.layer as s_layer
import synapse.lib.nexus as s_nexus
import synapse.lib.output as s_output
import synapse.lib.dyndeps as s_dyndeps
import synapse.lib.modules as s_modules
import synapse.lib.msgpack as s_msgpack
import synapse.lib.version as s_version
import synapse.lib.slabseqn as s_slabseqn
import synapse.lib.lmdbslab as s_lmdbslab
import synapse.lib.modelrev as s_modelrev

import synapse.tools.backup as s_backup

logger = logging.getLogger(__name__)

ALL_MIGROPS = (
    'dirn',
    'dmodel',
    'cell',
    'hiveauth',
    'hivestor',
    'hivelyr',
    'nodes',
    'nodedata',
    'cron',
    'triggers',
)

ADD_MODES = (
    'nexus',    # Layer.storNodeEdits() w/nexus
    'nonexus',  # Layer._storNodeEdit() w/o nexus
    'editor',   # Layer.editors[<op>]() w/o nexus
)

MAX_01X_VERS = (0, 1, 3)

class MigrAuth:
    '''
    Loads the Hive auth tree from 0.1.x and translates it to 0.2.x.

    Instance representation of auth:
        usersbyname (dict): { <name>: <iden>, ... }
        usersbyiden (dict): {
            <iden>: {
                'roles': [<roleiden1>, <roleiden2>, ...],
                'rules': [<ruletuple1>, <ruletuple2>, ...],
                ...
                <other user auth k,v pairs, e.g. admin, profile, etc.>,
            }
            ...
        }

        rolesbyname (dict): { <name>: <iden>, ... }
        rolesbyiden (dict): {
            <iden>: {
                'rules': [<ruletuple1>, <ruletuple2>, ...]
            },
            ...
        }

        authgatesbyname (dict): { <name e.g. view>: [<iden1>, <iden2>, ...], ... }
        authgatesbyiden (dict): {
            <iden>: {
                'rolesbyiden': {
                    <roleiden>: {
                        'rules': [<ruletuple1>, <ruletuple2>, ...]
                    },
                    ...
                },
                'usersbyiden': {
                    <useriden>: <user authgate dict, e.g. admin (a subset of user auth dict)>,
                    ...
                }
            },
            ...
        }
    '''
    def __init__(self, srctree, defaultview, triggers, queues, crons):
        self.defaultview = defaultview
        self.triggers = triggers
        self.queues = queues
        self.crons = crons
        self.srctree = srctree
        self.desttree = None

        self.usersbyiden = {}
        self.rolesbyiden = {}
        self.authgatesbyiden = {}
        self.usersbyname = {}
        self.rolesbyname = {}
        self.authgatesbyname = collections.defaultdict(list)

        self.userhierarchy = {}

    async def translate(self):
        '''
        Execute data translation steps, finishing with a 0.2.x hive auth tree

        Returns:
            (dict): Hive auth tree representation
        '''
        await self._srcReadTree()
        await self._trnAuth()
        await self._destCreateTree()
        await self._loadUserHierarchy()
        return self.desttree

    async def _loadUserHierarchy(self):
        '''
        Convenience representation of a user's permission hierarchy which is defined by the rules
        and roles for a given user, and the rules and roles for a given object (authgate).

            (<user iden 1>, <user name 1>): {
                (<obj iden>, <obj name>): set(
                    <object role rules>,
                    <user rules -- if admin applies to all objects>
                    <object user rules -- if admin applies to object>,
                    <user role rules>
                )
                ...
            }
        '''
        hierarchy = {}

        # reverse lookups for name by iden
        uname_lookup = {v: k for k, v in self.usersbyname.items()}
        aname_lookup = {i: k for k, v in self.authgatesbyname.items() for i in v}

        for uiden, uvals in self.usersbyiden.items():
            uname = uname_lookup.get(uiden)
            isadmin = False

            # user rules or admin
            urules = set()
            if uvals.get('admin'):
                isadmin = True
                urules.add(('admin', ))
            else:
                for allow, rule in uvals.get('rules', ()):
                    if allow:
                        urules.add(rule)

                # user role rules
                for riden in uvals.get('roles', ()):
                    for allow, rule in self.rolesbyiden[riden].get('rules', ()):
                        if allow:
                            urules.add(rule)

            hierarchy[(uiden, uname)] = {}

            # iterate over authgates
            for aiden, avals in self.authgatesbyiden.items():
                aname = aname_lookup[aiden]

                objrules = set()

                if isadmin:
                    objrules.update(urules)

                else:
                    # authgate user rules
                    if avals['usersbyiden'].get(uiden, {}).get('admin'):
                        objrules.add(('admin', ))

                    else:
                        objrules.update(urules)

                        for allow, rule in avals['usersbyiden'].get(uiden, {}).get('rules', ()):
                            if allow:
                                objrules.add(rule)

                        # authgate role rules
                        for riden, rvals in avals['rolesbyiden'].items():
                            for allow, rule in rvals.get('rules', ()):
                                if allow:
                                    objrules.add(rule)

                hierarchy[(uiden, uname)][(aiden, aname)] = tuple(objrules)

        self.userhierarchy = hierarchy

    async def _srcReadTree(self):
        '''
        Load source hive auth tree into a flatter local structure
        '''
        for uiden, uvals in self.srctree['kids']['users']['kids'].items():
            self.usersbyiden[uiden] = await self._srcReadUserKids(uvals['kids'])
            self.usersbyname[uvals['value']] = uiden

        for riden, rvals in self.srctree['kids'].get('roles', {}).get('kids', {}).items():
            self.rolesbyiden[riden] = {
                'rules': list(rvals.get('kids', {}).get('rules', {}).get('value', []))
            }
            name = rvals['value']
            if name is not None:
                self.rolesbyname[name] = riden

        for giden, gvals in self.srctree['kids']['authgates']['kids'].items():
            authgate = {
                'rolesbyiden': {},
                'usersbyiden': {},
            }

            for riden, rvals in gvals.get('kids', {}).get('roles', {}).get('kids', {}).items():
                authgate['rolesbyiden'][riden] = {
                    'rules': list(rvals.get('kids', {}).get('rules', {}).get('value', []))
                }

            for uiden, uvals in gvals.get('kids', {}).get('users', {}).get('kids', {}).items():
                authgate['usersbyiden'][uiden] = await self._srcReadUserKids(uvals.get('kids', {}))

            self.authgatesbyiden[giden] = authgate
            self.authgatesbyname[gvals['value']].append(giden)

    async def _srcReadUserKids(self, ukids):
        '''
        Iterate over user kid properties and return flat dict.

        Args:
            ukids: Hive tree representation of user kids

        Returns:
            (dict): Flat k, value pairs
        '''
        udict = {}
        for valname, val in ukids.items():
            value = val['value']
            if isinstance(value, tuple):
                value = list(value)
            udict[valname] = value

        return udict

    async def _trnAuth(self):
        '''
        Modify auth properties to translate to 0.2.x syntax.
        '''
        await self._trnAuthRoles()
        await self._trnAuthUsers()
        await self._trnAuthGates()

    async def _trnAuthRules(self, rules):
        '''
        Generic rule translations that need to occur for any rule set.

        Actions:
            - Convert ('storm', 'queue', ...) rules to ('queue', ...)
            - Convert node/prop/tag rules to node.foo.bar format (assumes these are in 0th position)
        '''
        for i, rule in enumerate(rules):
            if rule[1][:2] == ('storm', 'queue'):
                rules[i] = (rule[0], rule[1][1:])

            elif any([rule[1][0].startswith(m) for m in ('prop:', 'tag:')]):
                rules[i] = (rule[0], tuple(['node'] + rule[1][0].split(':') + list(rule[1][1:])))

            elif any([rule[1][0].startswith(m) for m in ('node:', 'layer:')]):
                rules[i] = (rule[0], tuple(rule[1][0].split(':') + list(rule[1][1:])))

        return rules

    async def _trnAuthRoles(self):
        '''
        Actions:
            - Add 'all' role with no rules if it doesn't exist
            - Convert rules
        '''
        for riden, rvals in self.rolesbyiden.items():
            rvals['rules'] = await self._trnAuthRules(rvals.get('rules', []))

        if 'all' not in self.rolesbyname:
            iden = s_common.guid()
            self.rolesbyname['all'] = iden
            self.rolesbyiden[iden] = {'rules': []}

    async def _trnAuthUsers(self):
        '''
        Actions:
            - Add 'all' role each user
            - Convert rules
        '''
        allrole = self.rolesbyname['all']
        for uiden, uvals in self.usersbyiden.items():
            roles = uvals.get('roles', [])
            roles.append(allrole)
            uvals['roles'] = roles

            uvals['rules'] = await self._trnAuthRules(uvals.get('rules', []))

    async def _trnAuthGates(self):
        '''
        Actions:
            - Convert rules
            - Create cortex authgate with no roles or users if it doesn't exist
            - Create trigger authgates with users=owner w/admin True
            - Create queue authgates with users=owner w/admin True
            - Create cron authgates with users=owner w/admin True
            - Add view:read rule to all role in defaultview
            - Add root user to all authgates (except cortex) if it doesn't exist
            - Change authgate name 'layr' to 'layer'
        '''
        for aiden, avals in self.authgatesbyiden.items():
            for riden, rvals in avals['rolesbyiden'].items():
                rvals['rules'] = await self._trnAuthRules(rvals.get('rules', []))
            for uiden, uvals in avals['usersbyiden'].items():
                uvals['rules'] = await self._trnAuthRules(uvals.get('rules', []))

        if 'cortex' not in self.authgatesbyname:
            self.authgatesbyname['cortex'].append('cortex')
            self.authgatesbyiden['cortex'] = {
                'rolesbyiden': {},
                'usersbyiden': {},
            }

        for tiden, uidens in self.triggers.items():
            self.authgatesbyname['trigger'].append(tiden)
            tusers = {}
            for uiden in uidens:
                tusers[uiden] = {'admin': True}

            self.authgatesbyiden[tiden] = {
                'rolesbyiden': {},
                'usersbyiden': tusers,
            }

        for qname, uiden in self.queues:
            qiden = f'queue:{qname}'
            self.authgatesbyname['queue'].append(qiden)
            self.authgatesbyiden[qiden] = {
                'rolesbyiden': {},
                'usersbyiden': {uiden: {'admin': True}},
            }

        for ciden, uiden in self.crons:
            self.authgatesbyname['cronjob'].append(ciden)
            self.authgatesbyiden[ciden] = {
                'rolesbyiden': {},
                'usersbyiden': {uiden: {'admin': True}},
            }

        alliden = self.rolesbyname['all']
        readrule = (True, ('view', 'read'))
        self.authgatesbyiden[self.defaultview]['rolesbyiden'][alliden] = {
            'rules': [readrule]
        }

        rootiden = self.usersbyname['root']
        for aiden, avals in self.authgatesbyiden.items():
            if aiden == 'cortex':
                continue

            if rootiden not in avals['usersbyiden']:
                avals['usersbyiden'][rootiden] = {
                    'admin': True
                }

        gates = self.authgatesbyname.pop('layr')
        self.authgatesbyname['layer'] = gates

    async def _destCreateTree(self):
        '''
        Load auth values into a data structure that can be loaded into the hive auth tree.
        '''
        gatekids = {}
        rolekids = {}
        userkids = {}

        # reverse lookups for name by iden
        uname_lookup = {v: k for k, v in self.usersbyname.items()}
        rname_lookup = {v: k for k, v in self.rolesbyname.items()}
        aname_lookup = {i: k for k, v in self.authgatesbyname.items() for i in v}

        # users
        for uiden, uvals in self.usersbyiden.items():
            uname = uname_lookup.get(uiden)
            if uname is None:
                logger.warning(f'Unable to match user iden to name: {uiden}')
            userkids[uiden] = {
                'kids': {k: {'value': v} for k, v in uvals.items()},
                'value': uname,
            }

        # load roles
        for riden, rvals in self.rolesbyiden.items():
            rname = rname_lookup.get(riden)
            rolekids[riden] = {
                'value': rname
            }
            if rvals.get('rules'):
                rolekids[riden]['kids'] = {
                    'rules': {
                        'value': rvals['rules'],
                    }
                }

        # load authgates and child roles, users
        for aiden, avals in self.authgatesbyiden.items():
            aroles = {
                'value': None,
                'kids': {}
            }
            for riden, rvals in avals['rolesbyiden'].items():
                rname = rname_lookup[riden]
                aroles['kids'][riden] = {
                    'value': rname
                }
                if rvals.get('rules'):
                    aroles['kids'][riden]['kids'] = {
                        'rules': {
                            'value': rvals['rules']
                        }
                    }

            ausers = {
                'value': None,
                'kids': {}
            }
            for uiden, uvals in avals['usersbyiden'].items():
                uname = uname_lookup.get(uiden)
                if uname is None:
                    logger.warning(f'Unable to match user iden to name: {uiden}')
                ausers['kids'][uiden] = {
                    'kids': {k: {'value': v} for k, v in uvals.items()},
                    'value': uname
                }

            gatekids[aiden] = {
                'kids': {
                    'roles': aroles,
                    'users': ausers,
                },
                'value': aname_lookup.get(aiden),
            }

        # store final tree
        self.desttree = {
            'value': None,
            'kids': {
                'authgates': {'kids': gatekids, 'value': None},
                'roles': {'kids': rolekids, 'value': None},
                'users': {'kids': userkids, 'value': None},
            }
        }

class Migrator(s_base.Base):
    '''
    Standalone tool for migrating Synapse from a source Cortex to a new destination 0.2.x Cortex.

    migrate() is the primary method which steps through sequential migration steps.
    The step is then carried out by a dedicated _migr* method which calls
    _src*, _trn*, _dest* methods as needed to read from the 0.1.x source, translate data to 0.2.x syntax,
    and finally write to the destination layer, respectively.

    Auth migration is handled through a standalone class MigrAuth.

    Source 0.1.x data is not modified, and migration can be run as a background operation.

    A migration dir is created to store stats, progress logs, checkpoints, and error logs specific to migration.
    '''
    async def __anit__(self, conf):
        await s_base.Base.__anit__(self)
        self.migrdir = 'migration'

        logger.debug(f'Migrator conf: {conf}')

        self.src = conf.get('src')
        self.dest = conf.get('dest')
        self.nodelim = conf.get('nodelim')

        self.migrops = conf.get('migrops')
        if self.migrops is None:
            self.migrops = ALL_MIGROPS

        self.addmode = conf.get('addmode')
        if self.addmode is None:
            self.addmode = 'nexus'

        if self.addmode not in ADD_MODES:
            raise Exception(f'addmode {self.addmode} is not valid')

        if self.addmode != 'nexus':
            logger.warning('Add mode is bypassing nexus - no migration splices will exist in 0.2.x cortex')

        self.editbatchsize = conf.get('editbatchsize')
        if self.editbatchsize is None:
            self.editbatchsize = 100

        self.fairiter = conf.get('fairiter')
        if self.fairiter is None:
            self.fairiter = 100

        self.safetyoff = conf.get('safetyoff', False)

        if self.safetyoff:
            logger.warning('Node value checking before addition has been disabled')

        self.fromlast = conf.get('fromlast', False)
        self.savechkpnt = 100000  # save a restart marker every this many nodes

        self.srcdedicated = conf.get('srcdedicated', False)

        self.destdedicated = conf.get('destdedicated', False)

        self.srcslabopts = {
            'readonly': True,
            'map_async': True,
            'readahead': False,
            'lockmemory': self.srcdedicated,
        }

        # data model
        self.model = None

        # storage
        self.migrslab = None
        self.migrdb = None
        self.nexusroot = None
        self.cellslab = None
        self.hivedb = None
        self.trigdb = None
        self.hive = None

    async def migrate(self):
        '''
        Execute the migration
        '''
        if self.dest is None:
            raise Exception('Destination dirn must be specified for migration.')

        # setup destination directory (migrop handled in method)
        locallyrs = await self._migrDirn()

        # initialize storage for migration
        await self._initStors()

        # check if configuration is valid to start
        isvalid = await self._chkValid()
        if not isvalid:
            return

        # migrate all of the config and hive data first so cortex is
        # in a valid state during node data migration
        if 'cell' in self.migrops:
            await self._migrCell()

        if 'dmodel' in self.migrops:
            await self._migrDatamodel()

        # migrop handled in method
        lyrs = {iden: await self._migrHiveLayerInfo(iden) for iden in locallyrs}

        if 'triggers' in self.migrops:
            await self._migrTriggers()

        if 'cron' in self.migrops:
            await self._migrCron()

        if 'hiveauth' in self.migrops:
            await self._migrHiveAuth()

        # full layer data migration
        for iden, migrlyrinfo in lyrs.items():
            logger.info(f'Starting migration for layer {iden}')
            wlyr = await self._destGetWlyr(self.dest, iden, migrlyrinfo)

            if 'nodes' in self.migrops:
                await self._migrNodes(iden, wlyr)

            if 'nodedata' in self.migrops:
                await self._migrNodeData(iden, wlyr)

        await self._dumpOffsets()
        await self._dumpVers()

    async def _dumpOffsets(self):
        '''
        Dump layer offsets into yaml file, overwriting if it exists.
        '''
        yamlout = {}
        async for offslog in self._migrlogGet('nodes', 'nextoffs'):
            yamlout[offslog['key']] = {
                'nextoffs': offslog['val'][0],
                'created': offslog['val'][1],
            }

        path = os.path.join(self.dest, self.migrdir, 'lyroffs.yaml')
        s_common.yamlsave(yamlout, path)

        logger.info(f'Saved layer offsets to {path}')

    async def _dumpVers(self):
        '''
        Dump cortex and model version info to yaml file; for dest only update migrops that took place.
        '''
        path = os.path.join(self.dest, self.migrdir, 'migrvers.yaml')
        if os.path.exists(path):
            yamlout = s_common.yamlload(path)
        else:
            yamlout = {
                'src:cortex': None,
                'src:model': {},
                'dest:cortex': {},
            }

        srcvers = await self._migrlogGetOne('chkvalid', 'vers', 'src:cortex')
        yamlout['src:cortex'] = srcvers.get('val') if srcvers is not None else None

        async for log in self._migrlogGet('nodes', 'vers'):
            yamlout['src:model'][log['key']] = log['val']  # lyriden: modelvers

        destvers = s_version.version
        for migrop in self.migrops:
            yamlout['dest:cortex'][migrop] = destvers

        s_common.yamlsave(yamlout, path)
        logger.info(f'Saved migration versions to {path}')

    async def dumpErrors(self):
        '''
        Fetch all node migration errors and dump to an mpk file.

        Returns:
            (str): File path
        '''
        path = os.path.join(self.dest, self.migrdir, 'migr.lmdb')
        if not os.path.exists(path):
            logger.error(f'Migration stor does not exist at {path}')
            return None

        # initialize migration data slab
        await self._initStors(migr=True, nexus=False, cell=False)

        logger.info('Starting dump of migration errors')
        errs = [err async for err in self._migrlogGet(migrop='nodes', logtyp='error')]
        dumpf = os.path.join(self.dest, self.migrdir, f'migrerrors_{s_common.now()}.mpk')
        with open(dumpf, 'wb') as fd:
            fd.write(s_msgpack.en(errs))

        return dumpf

    async def _initStors(self, migr=True, nexus=True, cell=True):
        '''
        Initialize required non-layer destination slabs for migration.
        '''
        # slab for tracking migration data
        if migr:
            path = os.path.join(self.dest, self.migrdir, 'migr.lmdb')
            if self.migrslab is None:
                self.migrslab = await s_lmdbslab.Slab.anit(path, map_async=True, readonly=False)
            self.migrdb = self.migrslab.initdb('migr')
            self.onfini(self.migrslab.fini)

        # optionally create migration nexus
        if nexus and self.addmode == 'nexus':
            path = os.path.join(self.dest)
            if self.nexusroot is None:
                self.nexusroot = await s_nexus.NexsRoot.anit(path)
            self.onfini(self.nexusroot.fini)

        # open cell
        if cell:
            path = os.path.join(self.dest, 'slabs', 'cell.lmdb')
            if self.cellslab is None:
                self.cellslab = await s_lmdbslab.Slab.anit(path, map_async=True, readonly=False)
            self.onfini(self.cellslab.fini)

            # triggers
            self.trigdb = self.cellslab.initdb('triggers')

            # hive
            self.hivedb = self.cellslab.initdb('hive')
            if self.hive is None:
                self.hive = await s_hive.SlabHive.anit(self.cellslab, db=self.hivedb)
            self.onfini(self.hive.root.fini)
            self.onfini(self.hive.fini)

        logger.debug('Finished storage initialization')
        return

    async def _chkValid(self):
        '''
        Check if the cortex is in a valid state to be migrated.

        Returns:
            (bool): Whether migration can proceed
        '''
        migrop = 'chkvalid'

        # remote layers
        lyrs = await self.hive.open(('cortex', 'layers'))
        for lyriden, lyrinfo in lyrs:
            lyrtype = lyrinfo.get('type')
            if lyrtype is not None and lyrtype.valu == 'remote':
                logger.error(f'{lyriden} is a remote layer - it must be unconfigured to proceed with migration')
                return False

        # check cortex version iff copied hive from src (otherwise will be 0.2.x after inplace migration)
        # currently only storing and not halting migration
        if 'dirn' in self.migrops:
            vers = await self.hive.get(('cellinfo', 'cortex:version'))
            if vers is None:
                vers = (-1, -1, -1)
                logger.warning(f'Unable to read src cortex version; consider upgrading before proceeding')
            await self._migrlogAdd(migrop, 'vers', 'src:cortex', vers)

        return True

    async def formCounts(self):
        '''
        Print form count comparison between source and destination.

        Returns:
            (list): List of formatted tables by layer as string
        '''
        fairiter = self.fairiter
        srclyrs = os.listdir(os.path.join(self.src, 'layers'))

        if self.dest is not None:
            destlyrs = os.listdir(os.path.join(self.dest, 'layers'))
        else:
            destlyrs = []

        outs = []
        for iden in srclyrs:
            hasdest = True
            if iden not in destlyrs:
                logger.warning(f'Layer {iden} not present in destination')
                hasdest = False

            # open source slab
            src_path = os.path.join(self.src, 'layers', iden, 'layer.lmdb')
            src_slab = await s_lmdbslab.Slab.anit(src_path, **self.srcslabopts)
            self.onfini(src_slab.fini)
            src_bybuid = src_slab.initdb('bybuid')

            src_fcnt = collections.defaultdict(int)
            src_tot = 0
            async for form in self._srcIterForms(src_slab, src_bybuid):
                src_fcnt[form] += 1
                src_tot += 1
                if src_tot % fairiter == 0:
                    await asyncio.sleep(0)
                if src_tot % 10000000 == 0:  # pragma: no cover
                    logger.debug(f'...counted {src_tot} nodes so far')

            await src_slab.fini()

            # open dest slab
            if hasdest:
                destpath = os.path.join(self.dest, 'layers', iden, 'layer_v2.lmdb')
                destslab = await s_lmdbslab.Slab.anit(destpath, lockmemory=False, readonly=True)
                self.onfini(destslab.fini)
                dest_fcnt = await destslab.getHotCount('count:forms')
                dest_fcnt = dest_fcnt.pack()
            else:
                dest_fcnt = {}

            outs.append(await self._getFormCountsPrnt(iden, src_fcnt, dest_fcnt))

        return outs

    async def _getFormCountsPrnt(self, iden, src_fcnt, dest_fcnt, addlog=None, resumed=False):
        '''
        Create pretty-printed form counts table and optionally save as log entries.

        Args:
            iden (str): Layer identifier for counts
            src_fcnt (dict): Dictionary of form name : source counts
            dest_fcnt (dict):  Dictionary of form name : dest counts
            addlog (str or None): Optionally add form count logs for a migrop
            resumed (bool): Whether stats are incremental from a resume and need to be added to existing on save

        Returns:
            (str): Pretty print-able form count comparison table
        '''
        rprt = [
            '\n',
            f'Form counts for layer {iden}:',
            f'{"FORM":<35}{"SRC_CNT":<15}{"DEST_CNT":<15}{"DIFF":<15}',
        ]
        src_tot = 0
        dest_tot = 0
        diff_tot = 0
        for form in set(list(src_fcnt.keys()) + list(dest_fcnt.keys())):
            scnt = src_fcnt.get(form, 0)
            src_tot += scnt

            dcnt = dest_fcnt.get(form, 0)
            dest_tot += dcnt

            diff = dcnt - scnt
            diff_tot += diff

            rprt.append(f'{form:<35}{scnt:<15}{dcnt:<15}{diff:<15}')
            if addlog is not None:
                log = await self._migrlogGetOne(addlog, 'stat', f'{iden}:form:{form}')
                scnt_prev, dcnt_prev = log['val'] if (log is not None and resumed) else (0, 0)

                # dest cnts don't get incremented since they are full layer hot counts
                await self._migrlogAdd(addlog, 'stat', f'{iden}:form:{form}', (scnt + scnt_prev, dcnt))

        rprt.append(f'{"TOTAL":<35}{src_tot:<15}{dest_tot:<15}{diff_tot:<15}')
        rprt.append('\n')
        prprt = '\n'.join(rprt)

        return prprt

    #############################################################
    # Migration operations
    #############################################################

    async def _migrDirn(self):
        '''
        Setup the destination cortex dirn.  If dest already exists it will not be overwritten.
        Copies all data *except* the layers

        Returns:
            (list): Discovered local physical layers
        '''
        migrop = 'dirn'

        dest = self.dest
        src = self.src
        logger.info(f'Starting cortex dirn migration: {src} to {dest}')

        lyrdir = os.path.join(src, 'layers')
        locallyrs = []
        for item in os.listdir(lyrdir):
            if os.path.isdir(os.path.join(lyrdir, item)):
                locallyrs.append(item)

        logger.info(f'Found {len(locallyrs)} src physical layers.')
        logger.debug(f'Source layers: {locallyrs}')

        destexists = os.path.exists(dest)

        if 'dirn' not in self.migrops:
            logger.info(f'Skipping dirn migration step; dest exists={destexists}')
            return locallyrs

        if not destexists:
            s_common.gendir(dest)

        for sdir in os.listdir(src):
            spath = os.path.join(src, sdir)
            dpath = os.path.join(dest, sdir)

            isdir = os.path.isdir(spath)
            isfile = os.path.isfile(spath)
            exists = os.path.exists(dpath)

            if sdir == 'layers':
                # make locallyr dirs if they don't exist but never overwrite
                for lyr in locallyrs:
                    lpath = os.path.join(dpath, lyr)
                    if not os.path.exists(lpath):
                        os.makedirs(lpath)
                    else:
                        logger.info(f'Layer dir exists, leaving as-is: {lyr}')

            elif isfile:
                if exists:
                    os.remove(dpath)
                shutil.copy(spath, dpath)

            elif spath.endswith('axon'):
                if exists:
                    shutil.rmtree(dpath)
                s_backup.backup(spath, dpath)

            elif spath.endswith('slabs'):
                # delete the non-nexus items from the destination if they exist
                if exists:
                    for root, dnames, fnames in os.walk(dpath, topdown=True):
                        for fname in fnames:
                            if 'nexus' not in fname:
                                os.remove(os.path.join(dpath, fname))
                        for dname in list(dnames):
                            if 'nexus' not in dname:
                                shutil.rmtree(os.path.join(dpath, dname))
                            dnames.remove(dname)

                s_backup.backup(spath, dpath)  # so we compress the slabs

            elif isdir:
                if exists:
                    shutil.rmtree(dpath)
                shutil.copytree(spath, dpath, ignore=shutil.ignore_patterns('sock'))

        logger.info(f'Completed dirn copy from {src} to {dest}')
        return locallyrs

    async def _migrCell(self):
        '''
        Migrate top-level cell information including the YAML file if it exists to
        remove deprecated confdefs.
        '''
        migrop = 'cell'

        # Set cortex:version to latest
        await self.hive.set(('cellinfo', 'cortex:version'), s_version.version)

        # confdefs
        validconfs = s_cortex.Cortex.confdefs
        yamlpath = os.path.join(self.dest, 'cell.yaml')
        if os.path.exists(yamlpath):
            conf = s_common.yamlload(self.dest, 'cell.yaml')
            remconfs = [k for k in conf.keys() if k not in validconfs]
            conf = {k: v for k, v in conf.items() if k not in remconfs}
            s_common.yamlsave(conf, self.dest, 'cell.yaml')

            logger.info(f'Completed cell migration, removed deprecated confdefs: {remconfs}')
            await self._migrlogAdd(migrop, 'prog', 'none', s_common.now())

    async def _migrDatamodel(self):
        '''
        Load datamodel in order to fetch stortypes.
        Currently no data modification occuring.
        '''
        migrop = 'dmodel'

        self.model = s_datamodel.Model()

        # load core modules
        mods = list(s_modules.coremods)
        mdefs = []
        for mod in mods:
            modu = s_dyndeps.tryDynLocal(mod)
            mdefs.extend(modu.getModelDefs(self))  # probably not the self its expecting...

        self.model.addDataModels(mdefs)

        # load custom modules
        # check for cell.yaml first otherwise an empty file will be created by yamlload
        yamlpath = os.path.join(self.dest, 'cell.yaml')
        if os.path.exists(yamlpath):
            conf = s_common.yamlload(self.dest, 'cell.yaml')
            if conf is not None:
                mdefs = []
                for mod in conf.get('modules', []):
                    modu = s_dyndeps.tryDynLocal(mod)
                    mdefs.extend(modu.getModelDefs(self))

                self.model.addDataModels(mdefs)

        # load extended model
        extprops = await (await self.hive.open(('cortex', 'model', 'props'))).dict()
        extunivs = await (await self.hive.open(('cortex', 'model', 'univs'))).dict()
        exttagprops = await (await self.hive.open(('cortex', 'model', 'tagprops'))).dict()

        for form, prop, tdef, info in extprops.values():
            try:
                self.model.addFormProp(form, prop, tdef, info)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as e:  # pragma: no cover
                logger.warning(f'ext prop ({form}:{prop}) error: {e}')

        for prop, tdef, info in extunivs.values():
            try:
                self.model.addUnivProp(prop, tdef, info)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as e:  # pragma: no cover
                logger.warning(f'ext univ ({prop}) error: {e}')

        for prop, tdef, info in exttagprops.values():
            try:
                self.model.addTagProp(prop, tdef, info)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as e:  # pragma: no cover
                logger.warning(f'ext tag prop ({prop}) error: {e}')

        logger.info('Completed datamodel migration')
        await self._migrlogAdd(migrop, 'prog', 'none', s_common.now())

    async def _migrHiveLayerInfo(self, iden):
        '''
        As each layer is migrated update the hive info.

        Args:
            iden (str): Iden of the layer

        Returns:
            migrlyrinfo (HiveDict): Shadow copy Layer info for initialization in migration only
        '''
        migrop = 'hivelyr'

        # migration shadow copy
        migrlyrnode = await self.hive.open(('migr', 'layers', iden))
        migrlyrinfo = await migrlyrnode.dict()

        # get existing data from the hive
        lyrnode = await self.hive.open(('cortex', 'layers', iden))
        layrinfo = await lyrnode.dict()

        if 'hivelyr' not in self.migrops:
            for name, valu in layrinfo.items():
                await migrlyrinfo.set(name, valu)
            return layrinfo

        # owner -> creator
        creator = None
        owner = await layrinfo.pop('owner', default=None)
        if owner is None:
            owner = 'root'

        users = await self.hive.open(('auth', 'users'))
        usersd = await users.dict()
        for uiden, uname in usersd.items():
            if uname == owner:
                creator = uiden

        if creator is None:
            logger.error(f'Unable to convert user name {owner} to iden, layer {iden} not properly setup in Hive')
            return

        # remove unneeded 0.1.x keys
        srcconf = await layrinfo.pop('config')
        await layrinfo.pop('name')
        await layrinfo.pop('type')

        # dedicated -> lockmemory
        lockmemory = False
        if srcconf is not None:
            srcdedicated = srcconf.get('dedicated')
            if srcdedicated is not None:
                lockmemory = srcdedicated

        # update layer info for 0.2.x
        await layrinfo.set('iden', iden)
        await layrinfo.set('creator', creator)
        await layrinfo.set('readonly', False)
        await layrinfo.set('lockmemory', lockmemory)
        await layrinfo.set('logedits', True)
        await layrinfo.set('model:version', s_modelrev.maxvers)

        for name, valu in layrinfo.items():
            await migrlyrinfo.set(name, valu)

        logger.info('Completed Hive layer info migration')
        await self._migrlogAdd(migrop, 'prog', iden, s_common.now())

        return migrlyrinfo

    async def _migrHiveAuth(self):
        '''
        Inplace migration in the new destination cortex for auth/permissions.
        Needs be run after layer info, triggers, and crons are updated in the hive.
        '''
        migrop = 'hiveauth'

        # get queues that need authgates added (no data migration/translation required)
        path = os.path.join(self.dest, 'slabs', 'queues.lmdb')
        qslab = await s_lmdbslab.Slab.anit(path, map_async=True, lockmemory=False)
        self.onfini(qslab.fini)

        queues = []  # list of (<queue name>, <user iden>)
        multiqueue = await qslab.getMultiQueue('cortex:queue')
        for q in multiqueue.list():
            name = q.get('name')
            uiden = q.get('meta', {}).get('user')

            if name is None or uiden is None:
                err = {'err': f'Missing iden values for queue', 'queue': q}
                logger.warning(err)
                await self._migrlogAdd(migrop, 'error', f'queue:{name}', err)
                continue

            queues.append((name, uiden))

        logger.info(f'Found {len(queues)} queues to migrate to AuthGates')

        # get triggers that will need authgates added (in 020 format)
        triggers = collections.defaultdict(set)
        for viewiden, viewnode in await self.hive.open(('cortex', 'views')):
            for trigiden, trignode in await viewnode.open(('triggers',)):
                triggers[trigiden].add(trignode.valu.get('user'))

        # get cron jobs that need authgates added (in 020 format)
        crons = []  # list of (<cron iden>, <user iden>)
        for croniden, cronvals in (await self.hive.dict(('agenda', 'appts'))).items():
            crons.append((croniden, cronvals.get('creator')))

        defaultview = await self.hive.get(('cellinfo', 'defaultview'))

        srctree = await self.hive.saveHiveTree(('auth',))

        migrauth = MigrAuth(srctree, defaultview, triggers, queues, crons)
        desttree = await migrauth.translate()

        # save a backup then replace
        await self.hive.loadHiveTree(srctree, ('auth01x',))
        await self.hive.loadHiveTree(desttree, ('auth',))

        # save user hierarchy
        dumpf = os.path.join(self.dest, self.migrdir, f'authhier_{s_common.now()}.mpk')
        with open(dumpf, 'wb') as fd:
            fd.write(s_msgpack.en(migrauth.userhierarchy))
        logger.info(f'Saved msgpackd user hierarchy to {dumpf}')

        logger.info('Completed HiveAuth migration')
        await self._migrlogAdd(migrop, 'prog', 'none', s_common.now())

    async def _migrCron(self):
        '''
        Replaces 'useriden' with 'creator'
        '''
        migrop = 'cron'

        crons = await self.hive.open(('agenda', 'appts'))
        for croniden, cronnode in crons:
            info = cronnode.valu
            uiden = info.get('useriden')
            if uiden is not None:
                del info['useriden']
                info['creator'] = uiden
                await cronnode.set(info)

        logger.info('Completed Cron migration')
        await self._migrlogAdd(migrop, 'prog', 'none', s_common.now())

    async def _migrTriggers(self):
        '''
        Remove old trigger entries and store in the hive.
        Assumes that the storm queries are parseable.
        '''
        migrop = 'triggers'

        viewtrgs = {}

        scnt = 0
        dcnt = 0
        for iden, valu in self.cellslab.scanByFull(db=self.trigdb):
            scnt += 1
            ruledict = s_msgpack.un(valu)

            try:
                viewiden = ruledict.pop('viewiden')
                useriden = ruledict.pop('useriden')
            except KeyError:
                err = {'err': f'Missing iden values for trigger', 'rule': ruledict}
                logger.warning(err)
                trgiden = s_common.guid(ruledict)
                await self._migrlogAdd(migrop, 'error', trgiden, err)
                continue

            ruledict['user'] = useriden

            trgiden = s_common.guid(ruledict)
            ruledict['iden'] = trgiden

            trgdict = viewtrgs.get(viewiden)
            if trgdict is None:
                trgnode = await self.hive.open(('cortex', 'views', viewiden, 'triggers'))
                trgdict = await trgnode.dict()
                viewtrgs[viewiden] = trgdict

            await trgdict.set(trgiden, ruledict)
            dcnt += 1
            self.cellslab.pop(iden, db=self.trigdb)  # remove old trigger

        await self._migrlogAdd(migrop, 'stat', f'tottriggers', (scnt, dcnt))

        logger.info(f'Completed trigger migration for {dcnt} of {scnt}')
        await self._migrlogAdd(migrop, 'prog', 'none', s_common.now())

    async def _migrNodes(self, iden, wlyr):
        '''
        Migrate nodes for a given layer.
        All errors are logged and continued.

        Args:
            iden (str): Iden of the layer
            wlyr (Layer): 0.2.0 Layer to write to
        '''
        migrop = 'nodes'
        nodelim = self.nodelim
        editchnks = self.editbatchsize
        fairiter = self.fairiter
        chknodes = not self.safetyoff
        fromlast = self.fromlast
        savechkpnt = self.savechkpnt
        addmode = self.addmode
        model = self.model

        # see if there is a checkpoint to start from
        startfrom = 0
        if fromlast:
            log = await self._migrlogGetOne(migrop, 'chkpnt', iden)
            if log is None:
                logger.warning(f'Start from checkpoint was specified but no log found for layer {iden}')
            else:
                startfrom = log['val'][1]
                savetime = s_time.repr(log['val'][2])
                logger.info(f'Resuming migration from {startfrom} saved on {savetime} for layer {iden}')

        # open storage
        path = os.path.join(self.src, 'layers', iden, 'layer.lmdb')
        src_slab = await s_lmdbslab.Slab.anit(path, **self.srcslabopts)
        src_bybuid = src_slab.initdb('bybuid')  # <buid><prop>=<valu>
        self.onfini(src_slab.fini)

        # check and store model vers
        versbyts = src_slab.get(b'layer:model:version')
        if versbyts is None:
            vers = (-1, -1, -1)
        else:
            vers = s_msgpack.un(versbyts)
        await self._migrlogAdd(migrop, 'vers', iden, vers)

        # even after a partial migration this vers should not be updated to 020 since
        # layer:model:version is no longer used
        if vers != MAX_01X_VERS:
            raise Exception(f'Layer {iden} model version must be at latest 01x vers: {vers} != {MAX_01X_VERS}')

        # record offset
        path = os.path.join(self.src, 'layers', iden, 'splices.lmdb')
        if os.path.exists(path):
            spliceslab = await s_lmdbslab.Slab.anit(path, map_async=True, lockmemory=False)
            self.onfini(spliceslab.fini)
            splicelog = s_slabseqn.SlabSeqn(spliceslab, 'splices')
            nextindx = splicelog.index()
            logger.info(f'Saved splicelog next offset {nextindx} for layer {iden}')
            await spliceslab.fini()
        else:
            logger.warning(f'Splice slab not found for {iden}, setting sync offset to 0')
            nextindx = 0

        await self._migrlogAdd(migrop, 'nextoffs', iden, (nextindx, s_common.now()))

        # migrate data
        src_fcnt = collections.defaultdict(int)
        nodeedits = []
        t_strt = s_common.now()
        stot = 0
        fastfwd = fromlast  # so we can stop the comparison once caught up
        async for node in self._srcIterNodes(src_slab, src_bybuid):
            stot += 1

            if stot % 1000000 == 0:  # pragma: no cover
                logger.info(f'...on node {stot:,} for layer {iden}')

            if stot % fairiter == 0:
                await asyncio.sleep(0)

            if fastfwd:
                if stot < startfrom:
                    continue
                else:
                    fastfwd = False

            buid = node[0]
            form = node[1]['ndef'][0].replace('*', '')
            src_fcnt[form] += 1

            if nodelim is not None and stot >= nodelim:
                logger.warning(f'Stopping node migration due to reaching nodelim {stot}')
                # checkpoint is the next node to add
                await self._migrlogAdd(migrop, 'chkpnt', iden, (buid, stot, s_common.now()))
                stot -= 1  # for stats on last node to migrate
                break

            if stot % savechkpnt == 0:
                await self._migrlogAdd(migrop, 'chkpnt', iden, (buid, stot, s_common.now()))

            err, nodeedit = await self._trnNodeToNodeedit(node, model, chknodes)
            if err is not None:
                logger.warning(err)
                err['node'] = node
                logger.debug(err)
                await self._migrlogAdd(migrop, 'error', buid, err)
                continue

            nodeedits.append(nodeedit)
            if len(nodeedits) == editchnks:
                err = await self._destAddNodes(wlyr, nodeedits, addmode)

                if err is not None:
                    logger.warning(err)
                    for ne in nodeedits:
                        logger.debug(f'error nodeedit group item: {ne}')
                        await self._migrlogAdd(migrop, 'error', buid, err)

                nodeedits = []

        # add last edit chunk if needed
        if len(nodeedits) > 0:
            err = await self._destAddNodes(wlyr, nodeedits, addmode)

            if err is not None:
                logger.warning(err)
                for ne in nodeedits:
                    logger.debug(f'error nodeedit group item: {ne}')
                    await self._migrlogAdd(migrop, 'error', buid, err)

        # checkpoint on completion if not already created due to a nodelim
        if nodelim is None:
            await self._migrlogAdd(migrop, 'chkpnt', iden, (buid, stot, s_common.now()))

        t_end = s_common.now()
        t_dur = t_end - t_strt
        t_dur_s = int(t_dur / 1000) + 1

        # collect final destination form count stats
        dest_fcnt = await wlyr.getFormCounts()
        dtot = sum(dest_fcnt.values())

        # store and log creation stats
        prprt = await self._getFormCountsPrnt(iden, src_fcnt, dest_fcnt, addlog=migrop, resumed=startfrom)
        logger.debug(prprt)

        if startfrom:
            # modify stats save for resume
            log = await self._migrlogGetOne(migrop, 'stat', f'{iden}:totnodes')
            stot_prev, dtot_prev = log['val'] if log is not None else (0, 0)

            stot_inc = stot - startfrom
            dtot_inc = dtot - dtot_prev
        else:
            stot_inc = stot
            dtot_inc = dtot

        await self._migrlogAdd(migrop, 'stat', f'{iden}:totnodes', (stot, dtot))
        await self._migrlogAdd(migrop, 'stat', f'{iden}:duration', (stot_inc, t_dur))

        rate = round(stot_inc / t_dur_s)
        logger.info(f'Migrated {dtot_inc:,} of {stot_inc:,} nodes in {t_dur_s} seconds ({rate} nodes/s avg)')
        logger.info(f'Completed node migration for {iden}')
        await self._migrlogAdd(migrop, 'prog', iden, s_common.now())

        await src_slab.fini()

        return

    async def _migrNodeData(self, iden, wlyr):
        '''
        Migrate nodedata for a given layer.
        All errors are logged and continued.

        Args:
            iden (str): Iden of the layer
            wlyr (Layer): 0.2.0 Layer to write to
        '''
        migrop = 'nodedata'
        nodelim = self.nodelim
        editchnks = self.editbatchsize
        fairiter = self.fairiter
        addmode = self.addmode

        # open storage
        path = os.path.join(self.src, 'layers', iden, 'nodedata.lmdb')
        src_slab = await s_lmdbslab.Slab.anit(path, **self.srcslabopts)
        src_bybuid = src_slab.initdb('bybuid')
        self.onfini(src_slab.fini)

        # migrate data
        nodeedits = []
        t_strt = s_common.now()
        stot = 0
        dtot = 0
        async for nodedata in self._srcIterNodedata(src_slab, src_bybuid):
            stot += 1
            if nodelim is not None and stot >= nodelim:
                logger.warning(f'Stopping nodedata migration due to reaching nodelim {stot}')
                # checkpoint is the next node to add
                await self._migrlogAdd(migrop, 'chkpnt', iden, (nodedata, stot, s_common.now()))
                break

            if stot % 1000000 == 0:  # pragma: no cover
                logger.info(f'...on node {stot:,} for layer {iden}')

            if stot % fairiter == 0:
                await asyncio.sleep(0)

            nodeedit = await self._trnNodedataToNodeedit(nodedata)
            if nodeedit is None:
                continue

            nodeedits.append(nodeedit)
            if len(nodeedits) == editchnks:
                err = await self._destAddNodes(wlyr, nodeedits, addmode)

                if err is not None:
                    logger.warning(err)
                    for ne in nodeedits:
                        logger.debug(f'error nodeedit group item: {ne}')
                        await self._migrlogAdd(migrop, 'error', ne[0], err)
                else:
                    dtot += len(nodeedits)

                nodeedits = []

        # add last edit chunk if needed
        if len(nodeedits) > 0:
            err = await self._destAddNodes(wlyr, nodeedits, addmode)

            if err is not None:
                logger.warning(err)
                for ne in nodeedits:
                    logger.debug(f'error nodeedit group item: {ne}')
                    await self._migrlogAdd(migrop, 'error', ne[0], err)
            else:
                dtot += len(nodeedits)

        t_end = s_common.now()
        t_dur = t_end - t_strt
        t_dur_s = int(t_dur / 1000) + 1
        rate = int(stot / t_dur_s)

        logger.info(f'Migrated {dtot:,} of {stot:,} nodedata entries in {t_dur_s} seconds ({rate} nodes/s avg)')
        await self._migrlogAdd(migrop, 'stat', f'{iden}:totnodes', (stot, dtot))
        await self._migrlogAdd(migrop, 'stat', f'{iden}:duration', (stot, t_dur))

        logger.info(f'Completed nodedata migration for {iden}')
        await self._migrlogAdd(migrop, 'prog', iden, s_common.now())

        await src_slab.fini()

        return

    #############################################################
    # Migration logging / record keeping
    #############################################################

    async def _migrlogAdd(self, migrop, logtyp, key, val):
        '''
        Add an error record to the migration data
        '''
        try:
            if isinstance(key, bytes):
                bkey = key
            else:
                bkey = key.encode()

            lkey = migrop.encode() + b'\x00' + logtyp.encode() + b'\x00' + bkey
            lval = s_msgpack.en(val)

            self.migrslab.put(lkey, lval, overwrite=True, db=self.migrdb)

        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as e:  # pragma: no cover
            logger.exception(f'Unable to store migration log: {migrop}; {logtyp}; {key}; {val}')
            pass

    async def _migrlogGet(self, migrop=None, logtyp=None, key=None):
        '''
        Yields log messages optionally filtered by a set of lkey parameters.

        Yields:
            (list): List of dicts representing log message.
        '''
        if key is None:
            bkey = None
        elif isinstance(key, bytes):
            bkey = key
        else:
            bkey = key.encode()

        lprefbld = []
        if migrop is not None:
            lprefbld.append(migrop.encode())
            if logtyp is not None:
                lprefbld.append(logtyp.encode())
                if bkey is not None:
                    lprefbld.append(bkey)

        lpref = b'\x00'.join(lprefbld)

        for lkey, lval in self.migrslab.scanByPref(lpref, db=self.migrdb):
            splt = lkey.split(b'\x00')

            try:
                skey = splt[2].decode()
            except UnicodeDecodeError:
                skey = splt[2]

            yield {
                'migrop': splt[0].decode(),
                'logtyp': splt[1].decode(),
                'key': skey,
                'val': s_msgpack.un(lval),
            }

    async def _migrlogGetOne(self, migrop=None, logtyp=None, key=None):
        async for log in self._migrlogGet(migrop, logtyp, key):
            return log
        return None

    #############################################################
    # Source (0.1.x) operations
    #############################################################

    async def _srcPackNode(self, buid, ndef, props, tags, tagprops):
        '''
        Return a packaged node
        '''
        return (buid, {
            'ndef': ndef,
            'props': props,
            'tags': tags,
            'tagprops': tagprops,
        })

    async def _srcIterForms(self, buidslab, buiddb):
        '''
        Iterate only to retrieve literal node forms.

        Yields:
            (str): Form name for a unique node
        '''
        for lkey, _ in buidslab.scanByFull(db=buiddb):
            prop = lkey[32:].decode('utf8')
            if prop[0] == '*':
                yield prop[1:]

    async def _srcIterNodes(self, buidslab, buiddb):
        '''
        Yield node information directly from the 0.1.x source slab.

        Yields:
            (tuple):
                (<buid>, {
                    'ndef': (<formname>, <formvalu>),
                    'props': {<propname>: <propvalu>, ...},
                    'tags': {<tagname>: <tagvalu>, ...},
                    'tagprops': {
                        <tagname>: {<propname>: <propvalu>, ...},
                        ...
                    }
                )
        '''
        buid = None
        ndef = None
        props = {}
        tags = {}
        tagprops = collections.defaultdict(dict)
        for lkey, lval in buidslab.scanByFull(db=buiddb):
            rowbuid = lkey[0:32]
            prop = lkey[32:].decode('utf8')
            valu, indx = s_msgpack.un(lval)  # throwing away indx

            # new node; if not at start, yield the last node and reset
            if buid is not None and rowbuid != buid:
                yield await self._srcPackNode(buid, ndef, props, tags, tagprops)
                buid = None
                ndef = None
                props = {}
                tags = {}
                tagprops = collections.defaultdict(dict)

            if buid is None:
                buid = rowbuid

            # add node information
            if prop[0] == '*':
                if ndef is None:
                    ndef = (prop, valu)
                else:
                    props[prop] = valu

            elif prop[0] == '#':
                if ':' in prop:  # tagprop
                    tname, tprop = prop.split(':')
                    tagprops[tname][tprop] = valu
                else:
                    tags[prop] = valu

            else:
                props[prop] = valu

        # yield last node
        if buid is not None:
            yield await self._srcPackNode(buid, ndef, props, tags, tagprops)

    async def _srcIterNodedata(self, buidslab, buiddb):
        '''
        Iterate over 0.1.0 nodedata

        Yields:
            (tuple): buid, name, val
        '''
        for lkey, lval in buidslab.scanByFull(db=buiddb):
            yield lkey[:32], lkey[32:].decode(), s_msgpack.un(lval)

    #############################################################
    # Translation operations
    #############################################################

    async def _trnNodeToNodeedit(self, node, model, chknodes=True):
        '''
        Create translation of node info to an 0.2.0 node edit.

        Args:
            node (tuple): (<buid>, {'ndef': ..., 'props': ..., 'tags': ..., 'tagprops': ...}
            model (Obj): Datamodel instance
            chknodes (bool): Whether to require valid node norming and buid comparisons in order to add

        Returns:
            (tuple): (cond, nodedit)
                cond: None or error dict
                nodeedit: (<buid>, <form>, [edits]) where edits is list of (<type>, <info>)
        '''
        buid = node[0]
        fname = node[1]['ndef'][0]
        fval = node[1]['ndef'][1]

        if fname[0] == '*':
            fname = fname[1:]
        else:
            err = {'mesg': f'Unable to parse form name {fname}', 'node': node}
            return err, None

        edits = []

        # setup storage type
        if chknodes:
            try:
                mform = model.form(fname)
                stortype = mform.type.stortype
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as e:
                err = {'mesg': f'Unable to determine stortype for {fname}: {e}'}
                return err, None
        else:
            mform = None
            stortype = self._destGetFormStype(fname)
            if stortype is None:
                err = {'mesg': f'Unable to determine stortype for {fname}'}
                return err, None

        # safety check for buid/norming
        if chknodes:
            normerr = None
            try:
                formnorm, _ = mform.type.norm(fval)
                if fval != formnorm:
                    normerr = {'mesg': f'Normed form val does not match inbound {fname}, {fval}, {formnorm}'}
                if buid != s_common.buid((fname, fval)):
                    normerr = {'mesg': f'Calculated buid does not match inbound {buid}, {fname}, {fval}'}

            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as e:
                normerr = {'mesg': f'Buid/norming exception {e}: {buid}, {fname}, {fval}'}
                pass

            if normerr is not None:
                normerr['node'] = node
                return normerr, None

        edits.append((s_layer.EDIT_NODE_ADD, (fval, stortype)))  # name, stype

        # iterate over secondary properties
        for sprop, sval in node[1]['props'].items():
            sprop = sprop.replace('*', '')
            stortype = self._destGetPropStype(fname, sprop)
            if stortype is None:
                err = {'mesg': f'Unable to determine stortype for sprop {sprop}'}
                return err, None

            edits.append((s_layer.EDIT_PROP_SET, (sprop, sval, None, stortype)))  # name, valu, oldv, stype

        # set tags
        for tname, tval in node[1]['tags'].items():
            tnamenorm = tname[1:]
            edits.append((s_layer.EDIT_TAG_SET, (tnamenorm, tval, None)))  # tag, valu, oldv

        # tagprops
        for tname, tprops in node[1]['tagprops'].items():
            tnamenorm = tname[1:]

            for tpname, tpval in tprops.items():
                try:
                    tptype = model.tagprops.get(tpname)
                    stortype = tptype.base.stortype
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as e:
                    err = {'mesg': f'Unable to determine stortype for tagprop {tpname}: {e}'}
                    return err, None

                edits.append((s_layer.EDIT_TAGPROP_SET,
                              (tnamenorm, tpname, tpval, None, stortype)))  # tag, prop, valu, oldv, stype

        return None, (buid, fname, edits)

    async def _trnNodedataToNodeedit(self, nodedata):
        '''
        Create translation of node info to an 0.2.0 node edit.

        Args:
            nodedata (tuple): (<buid>, <name>, <val>)

        Returns:
            nodeedit (tuple): (<buid>, <form>, [edits]) where edits is list of (<type>, <info>)
        '''
        buid = nodedata[0]
        name = nodedata[1]
        valu = nodedata[2]

        edits = [(s_layer.EDIT_NODEDATA_SET, (name, valu, None))]

        return buid, None, edits

    #############################################################
    # Destination (0.2.0) operations
    #############################################################

    @s_cache.memoize(16)
    def _destGetFormStype(self, form):
        stortype = None
        try:
            mform = self.model.form(form)
            stortype = mform.type.stortype
        except Exception as e:
            logger.debug(f'Form stortype exception: {form}, {e}')
            pass
        return stortype

    @s_cache.memoize(32)
    def _destGetPropStype(self, form, sprop):
        stortype = None
        try:
            mform = self.model.form(form)
            prop = mform.prop(sprop)
            stortype = prop.type.stortype
        except Exception as e:
            logger.debug(f'Secondary prop stortype exception: {form}, {sprop}, {e}')
            pass
        return stortype

    async def _destGetWlyr(self, dirn, iden, migrlyrinfo):
        '''
        Get the write Layer object for the destination.

        Args:
            dirn (str): Cortex directory that contains the layer
            iden (str): iden of the layer to create object for
            migrlyrinfo (HiveDict): Layer information used to construct the write layer

        Returns:
            (synapse.lib.Layer): Write layer
        '''
        await migrlyrinfo.set('lockmemory', self.destdedicated)
        await migrlyrinfo.set('readonly', False)

        path = os.path.join(dirn, 'layers', iden)
        wlyr = await s_layer.Layer.anit(migrlyrinfo, path, nexsroot=self.nexusroot)
        self.onfini(wlyr.fini)

        return wlyr

    async def _destAddNodes(self, wlyr, nodeedits, addmode):
        '''
        Add nodes/nodedata to a write layer from nodeedits.

        Args:
            wlyr (synapse.lib.Layer): Layer to add node to
            nodeedits (list): list of nodeedits [ (<buid>, <form>, [edits]) ]
            addmode (str): One of ADD_MODES

        Returns:
            (dict or None): Error dict or None if successful
        '''
        meta = {'time': s_common.now(),
                'user': wlyr.layrinfo.get('creator'),
                }

        try:
            if addmode == 'nexus':
                await wlyr.storNodeEditsNoLift(nodeedits, meta)

            elif addmode == 'nonexus':
                for ne in nodeedits:
                    await wlyr._storNodeEdit(ne)

            elif addmode == 'editor':
                for ne in nodeedits:
                    buid, form, edits = ne
                    for edit in edits:
                        editor = edit[0]
                        if editor == s_layer.EDIT_NODE_ADD:
                            valu, stortype = edit[1]

                            byts = s_msgpack.en((form, valu, stortype))
                            if not wlyr.layrslab.put(buid + b'\x00', byts, db=wlyr.bybuid, overwrite=False):
                                continue

                            abrv = wlyr.getPropAbrv(form, None)
                            for indx in wlyr.getStorIndx(stortype, valu):
                                wlyr.layrslab.put(abrv + indx, buid, db=wlyr.byprop)

                            wlyr.formcounts.inc(form)

                            # bypasses setting .created to now()
                            # which would then be overwritten by EDIT_PROP_SET

                        else:
                            wlyr.editors[editor](buid, form, edit)

            else:
                err = {'mesg': f'Unrecognized addmode {addmode}'}
                return err

        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as e:
            lyriden = wlyr.iden
            err = {'mesg': f'Unable to store nodeedits on {lyriden}: {e}'}
            return err

        return None

async def main(argv, outp=s_output.stdout):
    desc = 'Tool for migrating Synapse Cortex storage from 0.1.x to 0.2.0'
    pars = argparse.ArgumentParser(prog='synapse.tools.migrate_020', description=desc)

    pars.add_argument('--src', required=True, type=str, help='Source cortex dirn to migrate from.')
    pars.add_argument('--dest', required=False, type=str, help='Destination cortex dirn to migrate to.')
    pars.add_argument('--migr-ops', required=False, type=str.lower, nargs='+', choices=ALL_MIGROPS,
                      help='Limit migration operations to run.')
    pars.add_argument('--nodelim', required=False, type=int,
                      help="Stop after migrating nodelim nodes")
    pars.add_argument('--add-mode', required=False, type=str.lower, default='nexus', choices=ADD_MODES,
                      help='Method to use for adding nodes.')
    pars.add_argument('--edit-batchsize', required=False, type=int, default=100,
                      help='Batch size for writing new nodeedits')
    pars.add_argument('--fair-iter', required=False, type=int, default=100,
                      help='Yield loop after so many node iters')
    pars.add_argument('--safety-off', required=False, action='store_true',
                      help='Do not check node values before adding')
    pars.add_argument('--from-last', required=False, action='store_true',
                      help='Start migration from the last node migrated (by count).')
    pars.add_argument('--src-dedicated', required=False, action='store_true',
                      help='Open source layer slab as dedicated (lockmemory=True).')
    pars.add_argument('--dest-dedicated', required=False, action='store_true',
                      help='Open destination layer slab as dedicated (lockmemory=True).')
    pars.add_argument('--log-level', required=False, default='info', choices=s_const.LOG_LEVEL_CHOICES,
                      help='Specify the log level', type=str.upper)
    pars.add_argument('--form-counts', required=False, action='store_true',
                      help='Print form count comparison betweeen src/dest (ignores any migration options).')
    pars.add_argument('--dump-errors', required=False, action='store_true',
                      help='Dump migration errors to an mpk file.')

    opts = pars.parse_args(argv)

    s_common.setlogging(logger, opts.log_level)

    dumperrors = opts.dump_errors
    formcounts = opts.form_counts
    dest = opts.dest

    conf = {
        'src': opts.src,
        'dest': dest,
        'migrops': opts.migr_ops,
        'nodelim': opts.nodelim,
        'addmode': opts.add_mode,
        'editbatchsize': opts.edit_batchsize,
        'fairiter': opts.fair_iter,
        'safetyoff': opts.safety_off,
        'fromlast': opts.from_last,
        'srcdedicated': opts.src_dedicated,
        'destdedicated': opts.dest_dedicated,
        'formcounts': formcounts,
    }

    migr = await Migrator.anit(conf=conf)

    try:
        if dumperrors:
            dumpf = await migr.dumpErrors()
            outp.printf(f'Dump file located at {dumpf}')

        elif formcounts:
            outs = await migr.formCounts()
            for out in outs:
                outp.printf(out)

        else:
            await migr.migrate()

        return migr

    finally:
        await migr.fini()

if __name__ == '__main__':  # pragma: no cover
    asyncio.run(s_base.main(main(sys.argv[1:])))
