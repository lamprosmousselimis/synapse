import os
import copy
import logging
import argparse
import collections.abc as c_abc

import yaml
import fastjsonschema

import synapse.exc as s_exc
import synapse.common as s_common

import synapse.lib.const as s_const
import synapse.lib.output as s_output
import synapse.lib.hashitem as s_hashitem

logger = logging.getLogger(__name__)

JS_VALIDATORS = {}

def getJsSchema(confbase, confdefs):
    '''
    Generate a Synapse JSON Schema for a Cell using a pair of confbase and confdef values.

    Args:
        confbase (dict): A JSON Schema dictionary of properties for the object. This content has
                         precedence over the confdefs argument.
        confdefs (dict): A JSON Schema dictionary of properties for the object.

    Notes:
        This generated a JSON Schema draft 7 schema for a single object, which does not allow for
        additional properties to be set on it.  The data in confdefs is implementer controlled and
        is welcome to specify

    Returns:
        dict: A complete JSON schema.
    '''
    props = {}
    schema = {
        '$schema': 'http://json-schema.org/draft-07/schema#',
        'additionalProperties': False,
        'properties': props,
        'type': 'object'
    }
    props.update(confdefs)
    props.update(confbase)
    return schema

def getJsValidator(schema):
    '''
    Get a fastjsonschema callable.

    Args:
        schema (dict): A JSON Schema object.

    Returns:
        callable: A callable function that can be used to validate data against the json schema.
    '''
    # It is faster to hash and cache the functions here than it is to
    # generate new functions each time we have the same schema.
    key = s_hashitem.hashitem(schema)
    func = JS_VALIDATORS.get(key)
    if func:
        return func
    func = fastjsonschema.compile(schema)
    JS_VALIDATORS[key] = func
    return func

jsonschematype2argparse = {
    'integer': int,
    'string': str,
    'boolean': bool,
    'number': float,
}

def make_envar_name(key, prefix=None):
    '''
    Convert a colon delimited string into an uppercase, underscore delimited string.

    Args:
        key (str): Config key to convert.
        prefix (str): Optional string prefix to prepend the the config key.

    Returns:
        str: The string to lookup against a envar.
    '''
    nk = f'{key.replace(":", "_")}'
    if prefix:
        nk = f'{prefix}_{nk}'
    return nk.upper()

class Config(c_abc.MutableMapping):
    '''
    Synapse configuration helper based on JSON Schema.

    Args:
        schema (dict): The JSON Schema (draft v7) which to validate
                       configuration data against.
        conf (dict): Optional, a set of configuration data to preload.
        envar_prefix (str): Optional, a prefix used when collecting configuration
                            data from environmental variables.

    Notes:
        This class implements the collections.abc.MutableMapping class, so it
        may be used where a dictionary would otherwise be used.

        The default values provided in the schema must be able to be recreated
        from the repr() of their Python value.

        Default values are not loaded into the configuration data until
        the ``reqConfValid()`` method is called.

    '''
    def __init__(self,
                 schema,
                 conf=None,
                 envar_prefix=None,
                 ):
        self.json_schema = schema
        if conf is None:
            conf = {}
        self.conf = conf
        self._argparse_conf_names = {}
        self.envar_prefix = envar_prefix
        self.validator = getJsValidator(self.json_schema)

    @classmethod
    def getConfFromCell(cls, cell, conf=None, envar_prefix=None):
        '''
        Get a Config object from a Cell directly (either the ctor or the instance thereof).

        Returns:
            Config: A Config object.
        '''
        schema = getJsSchema(cell.confbase, cell.confdefs)
        return cls(schema, conf=conf, envar_prefix=envar_prefix)

    # Argparse support methods
    def getArgumentParser(self, pars=None):
        '''
        Make or update an argparse.ArgumentParser with configuration switches.

        Args:
            pars (argparse.ArgumentParser): Optional, an existing argparser to update.

        Notes:
            Configuration data is placed in the argument group called ``config``.

        Returns:
            argparse.ArgumentParser: Either a new or the existing ArgumentParser.
        '''
        if pars is None:
            pars = argparse.ArgumentParser()
        agrp = pars.add_argument_group('config', 'Configuration arguments.')
        self._addArgparseArguments(agrp)
        return pars

    def _addArgparseArguments(self, pgrp):
        '''
        Do the work for adding arguments from the schema to an argumentgroup.

        Args:
            pgrp (argparse._ArgumentGroup): The argumentgroup which arguments are added to.

        Returns:
            None: Returns None.
        '''
        for (name, conf) in self.json_schema.get('properties').items():
            atyp = jsonschematype2argparse.get(conf.get('type'))
            if atyp is None:
                continue
            akwargs = {'help': conf.get('description', ''),
                       'action': 'store',
                       'type': atyp,
                       'default': s_common.novalu
                       }

            if atyp is bool:
                akwargs.pop('type')
                default = conf.get('default')
                if default is None:
                    logger.debug(f'Boolean type is missing default information. Will not form argparse for [{name}]')
                    continue
                default = bool(default)
                # Do not use the default value!
                if default:
                    akwargs['action'] = 'store_false'
                    akwargs['help'] = akwargs['help'] + \
                                      ' Set this option to disable this option.'
                else:
                    akwargs['action'] = 'store_true'
                    akwargs['help'] = akwargs['help'] + \
                                      ' Set this option to enable this option.'

            parsed_name = name.replace(':', '-')
            replace_name = name.replace(':', '_')
            self._argparse_conf_names[replace_name] = name
            argname = '--' + parsed_name
            pgrp.add_argument(argname, **akwargs)

    def setConfFromOpts(self, opts):
        '''
        Set the opts for a conf object from a namespace object.

        Args:
            opts (argparse.Namespace): A Namespace object made from parsing args with an ArgumentParser
            made with getArgumentParser.

        Returns:
            None: Returns None.
        '''
        opts_data = vars(opts)
        for k, v in opts_data.items():
            if v is s_common.novalu:
                continue
            nname = self._argparse_conf_names.get(k)
            if nname is None:
                continue
            self.setdefault(nname, v)

    # Envar support methods
    def setConfFromEnvs(self):
        '''
        Set configuration options from environmental variables.

        Notes:
            Environment variables are resolved from configuration options after doing the following transform:

            - Replace ``:`` characters with ``_``.
            - Add a config provided prefix, if set.
            - Uppercase the string.
            - Resolve the environmental variable
            - If the environmental variable is set, set the config value to the results of ``yaml.yaml_safeload()``
              on the value.

        Examples:

            For the configuration value ``auth:passwd``, the environmental variable is resolved as ``AUTH_PASSWD``.
            With the prefix ``cortex``, the the environmental variable is resolved as ``CORTEX_AUTH_PASSWD``.

        Returns:
            None: Returns None.
        '''
        for (name, info) in self.json_schema.get('properties', {}).items():
            envar = make_envar_name(name, prefix=self.envar_prefix)
            envv = os.getenv(envar)
            if envv is not None:
                envv = yaml.safe_load(envv)
                resp = self.setdefault(name, envv)
                if resp == envv:
                    logger.debug(f'Set config valu from envar: [{envar}]')

    # General methods
    def reqConfValid(self):
        '''
        Validate that the loaded configuration data is valid according to the schema.

        Notes:
            The validation set does set any default values which are not currently
            set for configuration options.

        Returns:
            None: This returns nothing.
        '''
        try:
            self.validator(self.conf)
        except fastjsonschema.exceptions.JsonSchemaException as e:
            logger.exception('Configuration is invalid.')
            raise s_exc.BadConfValu(mesg=f'Invalid configuration found: [{str(e)}]') from None
        else:
            return

    def reqConfValu(self, key):
        '''
        Get a configuration value.  If that value is not present in the schema
        or is not set, then raise an exception.

        Args:
            key (str): The key to require.

        Returns:
            The requested value.
        '''
        # Ensure that the key is in self.json_schema
        if key not in self.json_schema.get('properties', {}):
            raise s_exc.BadArg(mesg='Required key is not present in the configuration schema.',
                               key=key)

        # Ensure that the key is present in self.conf
        if key not in self.conf:
            raise s_exc.NeedConfValu(mesg='Required key is not present in configuration data.',
                                     key=key)

        return self.conf.get(key)

    def asDict(self):
        '''
        Get a copy of configuration data.

        Returns:
            dict: A copy of the configuration data.
        '''
        return copy.deepcopy(self.conf)

    # be nice...
    def __repr__(self):
        info = [self.__class__.__module__ + '.' + self.__class__.__name__]
        info.append(f'at {hex(id(self))}')
        info.append(f'conf={self.conf}')
        return '<{}>'.format(' '.join(info))

    # ABC methods
    def __len__(self):
        return len(self.conf)

    def __iter__(self):
        return self.conf.__iter__()

    def __delitem__(self, key):
        return self.conf.__delitem__(key)

    def __setitem__(self, key, value):
        # This explicitly doesn't do any type validation.
        # The type validation is done on-demand, in order to
        # allow a user to incrementally construct the config
        # from different sources before turning around and
        # doing a validation pass which may fail.
        return self.conf.__setitem__(key, value)

    def __getitem__(self, item):
        return self.conf.__getitem__(item)

def common_argparse(argp, https='4443', telep='tcp://0.0.0.0:27492/',
                    telen=None, cellname='Cell'):
    '''
    Add a set of common arguments to an ArgumentParser that can be used with ``common_cb``.

    Args:
        argp (argparse.ArgumentParser): ArgumentParser to augment.
        https (str): Port to listen to HTTPS on.
        telep (str): Telepath address to listen on.
        telen (str): Optional, name to share the cell as.
        cellname (str): Optional, name to inject into the ``--name`` help argument.

    Returns:
        None: Returns None.
    '''
    argp.add_argument('--https', default=https, dest='port',
                      type=int, help='The port to bind for the HTTPS/REST API.')
    argp.add_argument('--telepath', default=telep,
                      help='The telepath URL to listen on.')
    argp.add_argument('--name', default=telen,
                      help=f'The (optional) additional name to share the {cellname} as.')

async def common_cb(cell, opts, outp):
    '''
    A common base callback that can be used in conjunction with ``common_argparse``.

    Notes:
        This sets https server port, telepath listening port and a telepath share name if set.

    Args:
        cell: Synapse Cell.
        opts (argparse.Namespace): An argparse Namespace object from parsed arguments.
        outp (s_output.Output): Output object.

    Returns:
        None: Returns None.
    '''
    outp.printf(f'...{cell.getCellType()} API (telepath): %s' % (opts.telepath,))
    await cell.dmon.listen(opts.telepath)

    outp.printf(f'...{cell.getCellType()} API (https): %s' % (opts.port,))
    await cell.addHttpsPort(opts.port)

    if opts.name:
        outp.printf(f'...{cell.getCellType()} additional share name: {opts.name}')
        cell.dmon.share(opts.name, cell)

async def main(ctor,
               argv,
               pars=None,
               cb=None,
               outp=s_output.stdout,
               envar_prefix=None,
               ):
        '''
        Cell configuration launcher helper.

        Args:
            ctor: Synapse Cell ctor.
            argv (list): List of arguments to parse.
            pars (argparse.ArgumentParser): Optional, a user provided ArgumentParser. Useful when combined with the cb.
            cb (callable): Optional callback function which takes the cell, opts and outp as arguments.
            outp (s_output.Output): An output instance for printing output.
            envar_prefix (str): A envar prefix for collecting envar based configuration data.

        Notes:
            This does the following items:

                - Create a Config object from the Cell Ctor.
                - Create (or inject) argument options into an Argument Parser from the Config object.
                - Parses the provided arguments.
                - Sets logging for the process.
                - Loads configuration data from the parsed options and environment variables.
                - Creates the Cell from the Cell Ctor.
                - Executes the provided callback function.
                - Returns the Cell.

            Provided ArgumentParser instances will have the following argument injected into it in order
            to provide the location where the cell is started from, and to do default logging configuration.

            ::

                pars.add_argument('celldir', type=str,
                                  help='The directory for the Cell to use for storage.')
                pars.add_argument('--log-level', default='INFO',
                                  choices=s_const.LOG_LEVEL_CHOICES,
                                  help='Specify the Python logging log level.', type=str.upper)

        Returns:
            The Synapse Cell made from the provided Ctor.
        '''
        conf = Config.getConfFromCell(ctor, envar_prefix=envar_prefix)
        pars = conf.getArgumentParser(pars=pars)
        # Inject celldir & logging argument so we can rely on having it around.
        pars.add_argument('celldir', type=str,
                          help=f'The directory for the {ctor.getCellType()} to use for storage.')
        pars.add_argument('--log-level', default='INFO', choices=s_const.LOG_LEVEL_CHOICES,
                          help='Specify the Python logging log level.', type=str.upper)
        opts = pars.parse_args(argv)

        s_common.setlogging(logger, defval=opts.log_level)

        conf.setConfFromOpts(opts)
        conf.setConfFromEnvs()

        outp.printf(f'starting {ctor.getCellType()}: {opts.celldir}')

        cell = await ctor.anit(opts.celldir, conf=conf)

        try:
            if cb:
                await cb(cell, opts, outp)
        except Exception:
            await cell.fini()
            raise
        else:
            return cell
