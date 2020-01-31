import json
import queue
import shlex
import pprint
import logging

import synapse.exc as s_exc
import synapse.common as s_common

import synapse.lib.cli as s_cli
import synapse.lib.cmd as s_cmd
import synapse.lib.node as s_node
import synapse.lib.time as s_time
import synapse.lib.msgpack as s_msgpack

logger = logging.getLogger(__name__)

RED = '#ff0066'
YELLOW = '#f4e842'
BLUE = '#6faef2'

class Log(s_cli.Cmd):
    '''Add a storm log to the local command session.

Notes:
    By default, the log file contains all messages received from the execution of
    a Storm query by the current CLI. By default, these messages are saved to a
    file located in ~/.syn/stormlogs/storm_(date).(format).

Examples:
    # Enable logging all messages to mpk files (default)
    log --on

    # Disable logging and close the current file
    log --off

    # Enable logging, but only log splices. Log them as jsonl instead of mpk.
    log --on --splices-only --format jsonl

    # Enable logging, but log to a custom path:
    log --on --path /my/aweome/log/directory/storm20010203.mpk

    # Log only the node messages which come back from a storm cmd execution.
    log --on --nodes-only --path /my/awesome/log/directory/stormnodes20010203.mpk
    '''
    _cmd_name = 'log'
    _cmd_syntax = (
        ('line', {'type': 'glob'}),
    )

    splicetypes = (
        'tag:add',
        'tag:del',
        'node:add',
        'node:del',
        'prop:set',
        'prop:del',
        'tag:prop:set',
        'tag:prop:del',
    )

    def _make_argparser(self):

        parser = s_cmd.Parser(prog='log', outp=self, description=self.__doc__)
        muxp = parser.add_mutually_exclusive_group(required=True)
        muxp.add_argument('--on', action='store_true', default=False,
                          help='Enables logging of storm messages to a file.')
        muxp.add_argument('--off', action='store_true', default=False,
                          help='Disables message logging and closes the current storm file.')
        parser.add_argument('--format', choices=('mpk', 'jsonl'), default='mpk', type=str.lower,
                            help='The format used to save messages to disk. Defaults to msgpack (mpk).')
        parser.add_argument('--path', type=str, default=None,
                            help='The path to the log file.  This will append messages to a existing file.')
        optmux = parser.add_mutually_exclusive_group()
        optmux.add_argument('--splices-only', action='store_true', default=False,
                            help='Only records splices. Does not record any other messages.')
        optmux.add_argument('--nodes-only', action='store_true', default=False,
                            help='Only record the packed nodes returned by storm.')
        return parser

    def __init__(self, cli, **opts):
        s_cli.Cmd.__init__(self, cli, **opts)
        # Give ourselves a local ref to locs since we're stateful.
        self.locs = self._cmd_cli.locs
        self._cmd_cli.onfini(self.closeLogFd)

    def onStormMesg(self, mesg):
        self.locs.get('log:queue').put(mesg)

    @s_common.firethread
    def queueLoop(self):
        q = self.locs.get('log:queue')
        while not self._cmd_cli.isfini:
            try:
                mesg = q.get(timeout=2)
            except queue.Empty:
                continue
            smesg = mesg[1].get('mesg')
            self.save(smesg)

    def save(self, mesg):
        fd = self.locs.get('log:fd')
        spliceonly = self.locs.get('log:splicesonly')
        nodesonly = self.locs.get('log:nodesonly')
        if fd and not fd.closed:
            if spliceonly and mesg[0] not in self.splicetypes:
                return
            if nodesonly:
                if mesg[0] != 'node':
                    return
                mesg = mesg[1]
            try:
                buf = self.encodeMsg(mesg)
            except Exception as e:  # pragma: no cover
                logger.error('Failed to serialize message: [%s]', str(e))
                return
            fd.write(buf)

    def encodeMsg(self, mesg):
        '''Get byts for a message'''

        fmt = self.locs.get('log:fmt')
        if fmt == 'jsonl':
            s = json.dumps(mesg, sort_keys=True) + '\n'
            buf = s.encode()
            return buf

        elif fmt == 'mpk':
            buf = s_msgpack.en(mesg)
            return buf

        mesg = f'Unknown encoding format: {fmt}'
        raise s_exc.SynErr(mesg=mesg)

    def closeLogFd(self):
        self._cmd_cli.off('storm:mesg', self.onStormMesg)
        q = self.locs.pop('log:queue', None)
        if q is not None:
            self.printf('Marking log queue done')
        thr = self.locs.pop('log:thr', None)
        if thr:
            self.printf('Joining log thread.')
            thr.join(2)
        fp = self.locs.pop('log:fp', None)
        fd = self.locs.pop('log:fd', None)
        for key in list(self.locs.keys()):
            if key.startswith('log:'):
                self.locs.pop(key, None)
        if fd:
            try:
                self.printf(f'Closing logfile: [{fp}]')
                fd.close()
            except Exception as e:  # pragma: no cover
                self.printf(f'Failed to close fd: [{str(e)}]')

    def openLogFd(self, opts):
        opath = self.locs.get('log:fp')
        if opath:
            self.printf('Must call --off to disable current file before starting a new file.')
            return
        fmt = opts.format
        path = opts.path
        nodes_only = opts.nodes_only
        splice_only = opts.splices_only
        if not path:
            ts = s_time.repr(s_common.now(), True)
            fn = f'storm_{ts}.{fmt}'
            path = s_common.getSynPath('stormlogs', fn)
        self.printf(f'Starting logfile at [{path}]')
        q = queue.Queue()
        fd = s_common.genfile(path)
        # Seek to the end of the file. Allows a user to append to a file.
        fd.seek(0, 2)
        self.locs['log:fp'] = path
        self.locs['log:fd'] = fd
        self.locs['log:fmt'] = fmt
        self.locs['log:queue'] = q
        self.locs['log:thr'] = self.queueLoop()
        self.locs['log:nodesonly'] = nodes_only
        self.locs['log:splicesonly'] = splice_only
        self._cmd_cli.on('storm:mesg', self.onStormMesg)

    async def runCmdOpts(self, opts):

        line = opts.get('line', '')

        try:
            opts = self._make_argparser().parse_args(shlex.split(line))
        except s_exc.ParserExit:
            return

        if opts.on:
            return self.openLogFd(opts)

        if opts.off:
            return self.closeLogFd()

class StormCmd(s_cli.Cmd):
    '''
    Execute a storm query.

    Syntax:
        storm <query>

    Arguments:
        query: The storm query

    Optional Arguments:
        --hide-tags: Do not print tags.
        --hide-props: Do not print secondary properties.
        --hide-unknown: Do not print messages which do not have known handlers.
        --raw: Print the nodes in their raw format. This overrides --hide-tags and --hide-props.
        --debug: Display cmd debug information along with nodes in raw format. This overrides other display arguments.
        --path: Get path information about returned nodes.
        --show <names>: Limit storm events (server-side) to the comma sep list)
        --file <path>: Run the storm query specified in the given file path.
        --optsfile <path>: Run the query with the given options from a JSON file.
        --spawn: (EXPERIMENTAL!) Run the query within a spawned sub-process runtime (read-only).

    Examples:
        storm inet:ipv4=1.2.3.4
        storm --debug inet:ipv4=1.2.3.4

    '''

    _cmd_name = 'storm'
    _cmd_syntax = (
        ('--hide-tags', {}),  # type: ignore
        ('--show', {'type': 'valu'}),
        ('--file', {'type': 'valu'}),
        ('--optsfile', {'type': 'valu'}),
        ('--hide-props', {}),
        ('--hide-unknown', {}),
        ('--raw', {}),
        ('--debug', {}),
        ('--path', {}),
        ('--spawn', {'type': 'flag'}),
        ('--save-nodes', {'type': 'valu'}),
        ('query', {'type': 'glob'}),
    )

    def __init__(self, cli, **opts):
        s_cli.Cmd.__init__(self, cli, **opts)
        self.cmdmeths = {
            'node': self._onNode,
            'init': self._onInit,
            'fini': self._onFini,
            'print': self._onPrint,
            'warn': self._onWarn,
            'err': self._onErr
        }

    def _onNode(self, mesg):

        node = mesg[1]
        opts = node[1].pop('_opts', {})

        if opts.get('raw'):
            self.printf(repr(node))
            return

        formname, formvalu = s_node.reprNdef(node)

        self.printf(f'{formname}={formvalu}')

        if not opts.get('hide-props'):

            for name in sorted(s_node.props(node).keys()):

                valu = s_node.reprProp(node, name)

                if name[0] != '.':
                    name = ':' + name

                self.printf(f'        {name} = {valu}')

        if not opts.get('hide-tags'):

            for tag in sorted(s_node.tagsnice(node)):

                valu = s_node.reprTag(node, tag)
                tprops = s_node.reprTagProps(node, tag)
                printed = False
                if valu:
                    self.printf(f'        #{tag} = {valu}')
                    printed = True
                if tprops:
                    for prop, pval in tprops:
                        self.printf(f'        #{tag}:{prop} = {pval}')
                    printed = True
                if not printed:
                    self.printf(f'        #{tag}')

    def _onInit(self, mesg):
        tick = mesg[1].get('tick')
        if tick is not None:
            tick = s_time.repr(tick)
            self.printf(f'Executing query at {tick}')

    def _onFini(self, mesg):
        took = mesg[1].get('took')
        took = max(took, 1)

        count = mesg[1].get('count')
        pers = float(count) / float(took / 1000)
        self.printf('complete. %d nodes in %d ms (%d/sec).' % (count, took, pers))

    def _onPrint(self, mesg):
        self.printf(mesg[1].get('mesg'))

    def _onWarn(self, mesg):
        warn = mesg[1].get('mesg')
        self.printf(f'WARNING: {warn}', color=YELLOW)

    def _onErr(self, mesg):
        err = mesg[1]
        if err[0] == 'BadSyntax':
            pos = err[1].get('at', None)
            text = err[1].get('text', None)
            tlen = len(text)
            mesg = err[1].get('mesg', None)
            if pos is not None and text is not None and mesg is not None:
                text = text.replace('\n', ' ')
                # Handle too-long text
                if tlen > 60:
                    text = text[max(0, pos - 30):pos + 30]
                    if pos < tlen - 30:
                        text += '...'
                    if pos > 30:
                        text = '...' + text
                        pos = 33

                self.printf(text, color=BLUE)
                self.printf(f'{" "*pos}^', color=BLUE)
                self.printf(f'Syntax Error: {mesg}', color=RED)
                return

        self.printf(f'ERROR: {err}', color=RED)

    async def runCmdOpts(self, opts):

        text = opts.get('query')
        filename = opts.get('file')

        if bool(text) == bool(filename):
            self.printf('Cannot use a storm file and manual query together.')
            self.printf(self.__doc__)
            return

        if filename is not None:
            try:
                with open(filename, 'r') as fd:
                    text = fd.read()

            except FileNotFoundError:
                self.printf('file not found: %s' % (filename,))
                return

        stormopts = {}
        optsfile = opts.get('optsfile')
        if optsfile is not None:
            try:
                with open(optsfile) as fd:
                    stormopts = json.loads(fd.read())

            except FileNotFoundError:
                self.printf('optsfile not found: %s' % (optsfile,))
                return

        hide_unknown = opts.get('hide-unknown', self._cmd_cli.locs.get('storm:hide-unknown'))
        core = self.getCmdItem()

        stormopts.setdefault('repr', True)
        stormopts.setdefault('path', opts.get('path', False))

        showtext = opts.get('show')
        if showtext is not None:
            stormopts['show'] = showtext.split(',')

        if opts.get('spawn'):
            stormopts['spawn'] = True

        nodesfd = None
        if opts.get('save-nodes'):
            nodesfd = s_common.genfile(opts.get('save-nodes'))
            nodesfd.truncate(0)

        try:

            async for mesg in core.storm(text, opts=stormopts):

                await self._cmd_cli.fire('storm:mesg', mesg=mesg)

                if opts.get('debug'):
                    self.printf(pprint.pformat(mesg))

                else:

                    if mesg[0] == 'node':

                        if nodesfd is not None:
                            byts = json.dumps(mesg[1]).encode('utf8')
                            nodesfd.write(byts + b'\n')

                        # Tuck the opts into the node dictionary since
                        # they control node metadata display
                        mesg[1][1]['_opts'] = opts
                    try:
                        func = self.cmdmeths[mesg[0]]
                    except KeyError:
                        if hide_unknown:
                            continue
                        self.printf(repr(mesg))
                    else:
                        func(mesg)

        except s_exc.SynErr as e:

            if e.errinfo.get('errx') == 'CancelledError':
                self.printf('query canceled.')
                return

            raise

        finally:

            if nodesfd is not None:
                nodesfd.close()
