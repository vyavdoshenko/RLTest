from __future__ import print_function

import argparse
import io
import os
import cmd
import traceback
import sys
import shutil
import inspect
import unittest
import time
import shlex
import json
from multiprocessing import Process, Queue, set_start_method

from RLTest.env import Env, TestAssertionFailure, Defaults
from RLTest.utils import Colors, fix_modules, fix_modulesArgs
from RLTest.loader import TestLoader
from RLTest.Enterprise import binaryrepo
from RLTest import debuggers
from RLTest._version import __version__
from contextlib import redirect_stdout
from progressbar import progressbar, ProgressBar
import threading
import signal

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

RLTest_CONFIG_FILE_PREFIX = '@'
RLTest_CONFIG_FILE_NAME = 'config.txt'

class CustomArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwrags):
        super(CustomArgumentParser, self).__init__(*args, **kwrags)

    def convert_arg_line_to_args(self, line):
        for arg in shlex.split(line):
            if not arg.strip():
                continue
            if arg[0] == '#':
                break
            yield arg


class MyCmd(cmd.Cmd):

    def __init__(self, env):
        cmd.Cmd.__init__(self)
        self.env = env
        self.prompt = '> '
        try:
            commands_reply = env.cmd('command')
        except Exception:
            return
        commands = [c[0] for c in commands_reply]
        for c in commands:
            if type(c)==bytes:
                c=c.decode('utf-8')
            setattr(MyCmd, 'do_' + c, self._create_functio(c))

    def _exec(self, command):
        self.env.expect(*command).prettyPrint()

    def _create_functio(self, command):
        c = command
        return lambda self, x: self._exec([c] + shlex.split(x))

    def do_exec(self, line):
        self.env.expect(*shlex.split(line)).prettyPrint()

    def do_print(self, line):
        '''
        print
        '''
        print('print')

    def do_stop(self, line):
        '''
        print
        '''
        print('BYE BYE')
        return True

    def do_cluster_conn(self, line):
        '''
        move to oss-cluster connection
        '''
        if self.env.env == 'oss-cluster':
            self.env.con = self.env.envRunner.getClusterConnection()
            print('moved to cluster connection')
        else:
            print('cluster connection only available on oss-cluster env')

    def do_normal_conn(self, line):
        '''
        move to normal connection (will connect to the first shard on oss-cluster)
        '''
        self.env.con = self.env.envRunner.getConnection()
        print('moved to normal connection (first shard on oss-cluster)')

    do_exit = do_stop


parser = CustomArgumentParser(fromfile_prefix_chars=RLTest_CONFIG_FILE_PREFIX,
                              formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                              description='Test Framework for redis and redis module')
parser.add_argument(
    '--version', action='store_const', const=True, default=False,
    help='Print RLTest version and exit')

parser.add_argument(
    '--module', default=None, action='append',
    help='path to the module file. '
         'You can use `--module` more than once but it imples that you explicitly specify `--module-args` as well. '
         'Notice that on enterprise the file should be a zip file packed with [RAMP](https://github.com/RedisLabs/RAMP).')

parser.add_argument(
    '--module-args', default=None, action='append', nargs='*',
    help='arguments to give to the module on loading')

parser.add_argument(
    '--env', '-e', default='oss', choices=['oss', 'oss-cluster', 'enterprise', 'enterprise-cluster', 'existing-env', 'cluster_existing-env'],
    help='env on which to run the test')

parser.add_argument(
    '-p', '--redis-port', type=int, default=6379,
    help='Redis server port')

parser.add_argument(
    '--existing-env-addr', default='localhost:6379',
    help='Address of existing env, relevent only when running with existing-env, cluster_existing-env')

parser.add_argument(
    '--shards_ports',
    help=' list of ports, the shards are listening to, relevent only when running with cluster_existing-env')

parser.add_argument(
    '--cluster_address',
    help='enterprise cluster ip, relevent only when running with cluster_existing-env')

parser.add_argument(
    '--oss_password', default=None,
    help='set redis password, relevant for oss and oss-cluster environment')

parser.add_argument(
    '--cluster_node_timeout', default=5000,
    help='sets the node timeout on cluster in milliseconds')

parser.add_argument(
    '--cluster_credentials',
    help='enterprise cluster cluster_credentials "username:password", relevent only when running with cluster_existing-env')

parser.add_argument(
    '--internal_password', default='',
    help='Give an ability to execute commands on shards directly, relevent only when running with cluster_existing-env')

parser.add_argument(
    '--oss-redis-path', default='redis-server',
    help='path to the oss redis binary')

parser.add_argument(
    '--enterprise-redis-path', default=os.path.join(binaryrepo.REPO_ROOT, 'opt/redislabs/bin/redis-server'),
    help='path to the enterprise redis binary')

parser.add_argument(
    '--redis-config-file', default=None,
    help='path to the redis configuration file')

parser.add_argument(
    '--stop-on-failure', action='store_const', const=True, default=False,
    help='stop running on failure')

parser.add_argument(
    '-x', '--exit-on-failure', action='store_true',
    help='Stop test execution and exit on first assertion failure')

parser.add_argument(
    '--verbose', '-v', action='count', default=0,
    help='print more information about the test')

parser.add_argument(
    '--debug', action='store_const', const=True, default=False,
    help='stop before each test allow gdb attachment')

parser.add_argument(
    '-t', '--test', metavar='TEST', action='append', help='test to run, in the form of "file:test"')

parser.add_argument(
    '-f', '--tests-file', metavar='FILE', action='append', help='file containing test to run, in the form of "file:test"')

parser.add_argument(
    '-F', '--failed-tests-file', metavar='FILE', help='destination file for failed tests')

parser.add_argument(
    '--env-only', action='store_const', const=True, default=False,
    help='start the env but do not run any tests')

parser.add_argument(
    '--clear-logs', action='store_const', const=True, default=False,
    help='deleting the log directory before the execution')

parser.add_argument(
    '--log-dir', default='./logs',
    help='directory to write logs to')

parser.add_argument(
    '--log-level', default=None, metavar='LEVEL', choices=['debug', 'verbose', 'notice', 'warning'],
    help='sets the server log level')

parser.add_argument(
    '--use-slaves', action='store_const', const=True, default=False,
    help='run env with slaves enabled')

parser.add_argument(
    '--shards-count', default=1, type=int,
    help='Number shards in bdb')

parser.add_argument(
    '--test-timeout', default=0, type=int,
    help='Test timeout, 0 means no timeout.')

parser.add_argument(
    '--download-enterprise-binaries', action='store_const', const=True, default=False,
    help='run env with slaves enabled')

parser.add_argument(
    '--proxy-binary-path', default=os.path.join(binaryrepo.REPO_ROOT, 'opt/redislabs/bin/dmcproxy'),
    help='dmc proxy binary path')

parser.add_argument(
    '--enterprise-lib-path', default=os.path.join(binaryrepo.REPO_ROOT, 'opt/redislabs/lib/'),
    help='path of needed libraries to run enterprise binaries')

parser.add_argument(
    '-r', '--env-reuse', action='store_const', const=True, default=False,
    help='reuse exists env, this feature is based on best efforts, if the env can not be reused then it will be taken down.')

parser.add_argument(
    '--use-aof', action='store_const', const=True, default=False,
    help='use aof instead of rdb')

parser.add_argument(
    '--use-rdb-preamble', action='store_const', const=True, default=True,
    help='use rdb preamble when rewriting aof file')

parser.add_argument(
    '--debug-print', action='store_const', const=True, default=False,
    help='print debug messages')

parser.add_argument(
    '-V', '--vg', '--use-valgrind', action='store_const', const=True, default=False,
    dest='use_valgrind',
    help='running redis under valgrind (assuming valgrind is install on the machine)')

parser.add_argument(
    '--vg-suppressions', default=None, help='path valgrind suppressions file')
parser.add_argument(
    '--vg-options', default=None, dest='vg_options', help='valgrind [options]')
parser.add_argument(
    '--vg-no-leakcheck', action='store_true', help="Don't perform a leak check")
parser.add_argument(
    '--vg-verbose', action='store_true', help="Don't log valgrind output. "
                                              "Output to screen directly")
parser.add_argument(
    '--vg-no-fail-on-errors', action='store_true', dest='vg_no_fail_on_errors', help="Dont Fail test when valgrind reported any errors in the run."
                                                  "By default on RLTest the return value from Valgrind will be used to fail the tests."
                                                  "Use this option when you wish to dry-run valgrind but not fail the test on valgrind reported errors."
)

parser.add_argument(
    '--sanitizer', default=None, help='type of CLang sanitizer (addr|mem)')

parser.add_argument(
    '-i', '--interactive-debugger', action='store_const', const=True, default=False,
    help='runs the redis on a debuger (gdb/lldb) interactivly.'
         'debugger interactive mode is only possible on a single process and so unsupported on cluste or with slaves.'
         'it is also not possible to use valgrind on interactive mode.'
         'interactive mode direcly applies: --no-output-catch and --stop-on-failure.'
         'it is also implies that only one test will be run (if --env-only was not specify), an error will be raise otherwise.')

parser.add_argument('--debugger', help='Run specified command line as the debugger')

parser.add_argument(
    '-s', '--no-output-catch', action='store_const', const=True, default=False,
    help='all output will be written to the stdout, no log files. Implies --no-progress.')

parser.add_argument(
    '--no-progress', action='store_const', const=True, default=False,
    help='Do not show progress bar.')

parser.add_argument(
    '--verbose-information-on-failure', action='store_const', const=True, default=False,
    help='Print a verbose information on test failure')

parser.add_argument(
    '--enable-debug-command', action='store_const', const=True, default=False,
    help='On Redis 7, debug command need to be enabled in order to be used.')

parser.add_argument(
    '--enable-protected-configs', action='store_const', const=True, default=False,
    help='On Redis 7, this option needs to be enabled in order to change protected configuration in runtime.')

parser.add_argument(
    '--enable-module-command', action='store_const', const=True, default=False,
    help='On Redis 7, this option needs to be enabled in order to use module command (load/unload modules in runtime).')

parser.add_argument(
    '--allow-unsafe', action='store_const', const=True, default=False,
    help='On Redis 7, allow the three unsafe modes above (debug and module commands and protected configs)')

parser.add_argument('--check-exitcode', help='Check redis process exit code',
                    default=False, action='store_true')

parser.add_argument('--unix', help='Use Unix domain sockets instead of TCP',
                    default=False, action='store_true')

parser.add_argument('--randomize-ports',
                    help='Randomize Redis listening port assignment rather than'
                    'using default port',
                    default=False, action='store_true')

parser.add_argument('--parallelism', help='Run tests in parallel', default=1, type=int)

parser.add_argument(
    '--collect-only', action='store_true',
    help='Collect the tests and exit')

parser.add_argument('--tls', help='Enable TLS Support and disable the non-TLS port completely. TLS connections will be available at the default non-TLS ports.',
                    default=False, action='store_true')

parser.add_argument(
    '--tls-cert-file', default=None, help='/path/to/redis.crt')

parser.add_argument(
    '--tls-key-file', default=None, help='/path/to/redis.key')

parser.add_argument(
    '--tls-ca-cert-file', default=None, help='/path/to/ca.crt')

parser.add_argument(
    '--tls-passphrase', default=None, help='passphrase to use on decript key file')

class EnvScopeGuard:
    def __init__(self, runner):
        self.runner = runner

    def __enter__(self):
        pass

    def __exit__(self, type, value, traceback):
        self.runner.takeEnvDown()

class TestTimeLimit(object):
    """
    A test timeout watcher. The watcher opens thread that sleep for the
    required timeout and then wake up and send SIGUSR1 signal to the main thread
    causing it to enter a timeout phase. When enter a timeout phase, the main thread
    prints its trace and enter a deep sleep. The watcher thread continue collecting
    environment stats and when done kills the processes.
    """

    def __init__(self, timeout, timeout_func):
        self.timeout = timeout
        self.timeout_time = time.time() + self.timeout
        self.timeout_func = timeout_func
        self.condition = threading.Condition()
        self.thread = None
        self.is_done = False
        self.trace_printed = False

    def on_timeout(self, signum, frame):
        for line in traceback.format_stack():
            print(line.strip())
        self.trace_printed = True
        time.sleep(1000) # sleep forever process will be killed soon

    def watcher_thread(self):
        self.condition.acquire()
        while not self.is_done and self.timeout_time > time.time():
            self.condition.wait(timeout=0.1)
        if not self.is_done:
            print(Colors.Bred('Test Timeout, printing trace.'))
            os.kill(os.getpid(), signal.SIGUSR1)
            while not self.trace_printed:
                time.sleep(0.1)
            try:
                self.timeout_func()
            except Exception as e:
                print(Colors.Bred("Failed on timeout function, %s" % str(e)))
            os._exit(1)

    def reset(self):
        self.timeout_time = time.time() + self.timeout

    def __enter__(self):
        if self.timeout == 0:
            return self
        signal.signal(signal.SIGUSR1, self.on_timeout)
        self.thread = threading.Thread(target=self.watcher_thread)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.timeout == 0:
            return
        self.condition.acquire()
        self.is_done = True
        self.condition.notify(1)
        self.condition.release()


class RLTest:
    def __init__(self):
        # adding the current path to sys.path for test import puspused
        sys.path.append(os.getcwd())

        configFilePath = './%s' % RLTest_CONFIG_FILE_NAME
        if os.path.exists(configFilePath):
            args = ['%s%s' % (RLTest_CONFIG_FILE_PREFIX, RLTest_CONFIG_FILE_NAME)] + sys.argv[1:]
        else:
            args = sys.argv[1:]
        self.args = parser.parse_args(args=args)

        if self.args.version:
            print(Colors.Green('RLTest version {}'.format(__version__)))
            sys.exit(0)

        if self.args.redis_port not in range(1, pow(2, 16)):
            print(Colors.Bred(f'requested port {self.args.redis_port} is not valid'))
            sys.exit(1)

        if self.args.interactive_debugger:
            if self.args.env != 'oss' and not (self.args.env == 'oss-cluster' and Defaults.num_shards == 1) and self.args.env != 'enterprise':
                print(Colors.Bred('interactive debugger can only be used on non cluster env'))
                sys.exit(1)
            if self.args.use_valgrind:
                print(Colors.Bred('can not use valgrind with interactive debugger'))
                sys.exit(1)
            if self.args.use_slaves:
                print(Colors.Bred('can not use slaves with interactive debugger'))
                sys.exit(1)

            self.args.no_output_catch = True
            self.args.stop_on_failure = True

        if self.args.download_enterprise_binaries:
            br = binaryrepo.BinaryRepository()
            br.download_binaries()

        if self.args.clear_logs:
            if os.path.exists(self.args.log_dir):
                try:
                    shutil.rmtree(self.args.log_dir)
                except Exception as e:
                    print(e, file=sys.stderr)

        debugger = None
        if self.args.debugger:
            if self.args.env.endswith('existing-env'):
                print(Colors.Bred('can not use debug with existing-env'))
                sys.exit(1)
            debuggers.set_interactive_debugger(self.args.debugger)
            self.args.interactive_debugger = True
        if self.args.use_valgrind:
            if self.args.env.endswith('existing-env'):
                print(Colors.Bred('can not use valgrind with existing-env'))
                sys.exit(1)
            if self.args.vg_options is None:
                self.args.vg_options = os.getenv('VG_OPTIONS', '--leak-check=full --errors-for-leak-kinds=definite')
            vg_debugger = debuggers.Valgrind(options=self.args.vg_options,
                                             suppressions=self.args.vg_suppressions,
                                             fail_on_errors=not(self.args.vg_no_fail_on_errors),
                                             leakcheck=not(self.args.vg_no_leakcheck)
            )
            if self.args.vg_no_leakcheck:
                vg_debugger.leakcheck = False
            if self.args.no_output_catch or self.args.vg_verbose:
                vg_debugger.verbose = True
            debugger = vg_debugger
        elif self.args.interactive_debugger:
            debugger = debuggers.default_interactive_debugger

        sanitizer = None
        if self.args.sanitizer:
            sanitizer = self.args.sanitizer

        if self.args.env.endswith('existing-env'):
            # when running on existing env we always reuse it
            self.args.env_reuse = True

        # unless None, they must match in length
        if self.args.module_args:
            len_module_args = len(self.args.module_args)
            modules = self.args.module
            if type(modules) == list:
                if (len(modules) != len_module_args):
                    print(Colors.Bred('Using `--module` multiple time implies that you specify the `--module-args` in the the same number'))
                    sys.exit(1)

        if self.args.no_output_catch and self.args.parallelism > 1:
            print(Colors.Bred('--no-output-catch can not be combined with --parallelism.'))
            sys.exit(1)

        Defaults.module = fix_modules(self.args.module)
        Defaults.module_args = fix_modulesArgs(Defaults.module, self.args.module_args)
        Defaults.env = self.args.env
        Defaults.binary = self.args.oss_redis_path
        Defaults.verbose = self.args.verbose
        Defaults.logdir = self.args.log_dir
        Defaults.loglevel = self.args.log_level
        Defaults.use_slaves = self.args.use_slaves
        Defaults.num_shards = self.args.shards_count
        Defaults.shards_ports = self.args.shards_ports.split(',') if self.args.shards_ports is not None else None
        Defaults.cluster_address = self.args.cluster_address
        Defaults.cluster_credentials = self.args.cluster_credentials
        Defaults.internal_password = self.args.internal_password
        Defaults.proxy_binary = self.args.proxy_binary_path
        Defaults.re_binary = self.args.enterprise_redis_path
        Defaults.re_libdir = self.args.enterprise_lib_path
        Defaults.use_aof = self.args.use_aof
        Defaults.debug_pause = self.args.debug
        Defaults.debug_print = self.args.debug_print
        Defaults.no_capture_output = self.args.no_output_catch
        Defaults.print_verbose_information_on_failure = self.args.verbose_information_on_failure
        Defaults.debugger = debugger
        Defaults.sanitizer = sanitizer
        Defaults.exit_on_failure = self.args.exit_on_failure
        Defaults.port = self.args.redis_port
        Defaults.external_addr = self.args.existing_env_addr
        Defaults.use_unix = self.args.unix
        Defaults.randomize_ports = self.args.randomize_ports
        Defaults.use_TLS = self.args.tls
        Defaults.tls_cert_file = self.args.tls_cert_file
        Defaults.tls_key_file = self.args.tls_key_file
        Defaults.tls_ca_cert_file = self.args.tls_ca_cert_file
        Defaults.tls_passphrase = self.args.tls_passphrase
        Defaults.oss_password = self.args.oss_password
        Defaults.cluster_node_timeout = self.args.cluster_node_timeout
        Defaults.enable_debug_command = True if self.args.allow_unsafe else self.args.enable_debug_command
        Defaults.enable_protected_configs = True if self.args.allow_unsafe else self.args.enable_protected_configs
        Defaults.enable_module_command = True if self.args.allow_unsafe else self.args.enable_module_command
        Defaults.redis_config_file = self.args.redis_config_file

        if Defaults.use_unix and Defaults.use_slaves:
            raise Exception('Cannot use unix sockets with slaves')

        if Defaults.env == 'enterprise-cluster' and Defaults.redis_config_file is not None:
            raise Exception('Redis configuration file is not supported with enterprise-cluster env')

        self.tests = []
        self.testsFailed = {}
        self.currEnv = None
        self.loader = TestLoader()
        if self.args.test is not None:
            self.loader.load_spec(self.args.test)
        if self.args.tests_file is not None:
            for fname in self.args.tests_file:
                try:
                    with open(fname, 'r') as file:
                        for line in file.readlines():
                            line = line.strip()
                            if line.startswith('#') or line == "":
                                continue
                            try:
                                self.loader.load_spec(line)
                            except:
                                print(Colors.Red('Invalid test {TEST} in file {FILE}'.format(TEST=line, FILE=fname)))
                except:
                    print(Colors.Red('Test file {} not found'.format(fname)))
        if self.args.test is None and self.args.tests_file is None:
            self.loader.scan_dir(os.getcwd())

        if self.args.collect_only:
            self.loader.print_tests()
            sys.exit(0)
        if self.args.use_valgrind or self.args.check_exitcode:
            self.require_clean_exit = True
        else:
            self.require_clean_exit = False

        self.parallelism = self.args.parallelism

    def _convertArgsType(self):
        pass

    def stopEnvWithSegFault(self):
        if not self.currEnv:
            return
        self.currEnv.stopEnvWithSegFault()

    def takeEnvDown(self, fullShutDown=False):
        if not self.currEnv:
            return

        needShutdown = True
        if self.args.env_reuse and not fullShutDown:
            try:
                self.currEnv.flush()
                needShutdown = False
            except Exception as e:
                self.currEnv.stop()
                self.handleFailure(exception=e, testname='[env dtor]',
                                   env=self.currEnv)

        if needShutdown:
            flush_ok = True
            if self.currEnv.isUp():
                try:
                    self.currEnv.flush()
                except:
                    flush_ok = False
            self.currEnv.stop()
            if self.require_clean_exit and self.currEnv and (not self.currEnv.checkExitCode() or not flush_ok):
                print(Colors.Bred('\tRedis did not exit cleanly'))
                self.addFailure(self.currEnv.testName, ['redis process failure'])
                if self.args.check_exitcode:
                    raise Exception('Process exited dirty')
            self.currEnv = None

    def printException(self, err):
        msg = 'Unhandled exception: {}'.format(err)
        print('\t' + Colors.Bred(msg))
        traceback.print_exc(file=sys.stdout)

    def addFailuresFromEnv(self, name, env):
        """
        Extract the list of failures from the given test Env
        :param name: The name of the test that failed
        :param env: The Environment which contains the failures
        """
        if not env:
            self.addFailure(name, ['<unknown (environment destroyed)>'])
        else:
            self.addFailure(name, failures=env.assertionFailedSummary)

    def addFailure(self, name, failures=None):
        """
        Adds a list of failures to the report
        :param name: The name of the test that has failures
        :param failures: A string or of strings describing the individual failures
        """
        if failures and not isinstance(failures, (list, tuple)):
            failures = [failures]
        if not failures:
            failures = []
        self.testsFailed.setdefault(name, []).extend(failures)

    def getFailedTestsCount(self):
        return len(self.testsFailed)

    def handleFailure(self, testFullName=None, exception=None, prefix='', testname=None, env=None, error_msg=None):
        """
        Failure omni-function.

        This function handles failures given a set of input parameters.
        At least one of these must not be empty
        :param exception: The exception to report, of any
        :param prefix: The prefix to use for logging.
            This is usually the test name
        :param testname: The test name, use for recording the failures
        :param env: The environment, used for extracting failed assertions
        """
        if not testname and env:
            testname = env.testName
        elif not testname:
            if prefix:
                testname = prefix
            else:
                testname = '<unknown>'

        if exception:
            self.printError(testFullName if testFullName is not None else '')
            self.printException(exception)
        else:
            self.printFail(testFullName if testFullName is not None else '')

        if env:
            self.addFailuresFromEnv(testname, env)
        elif exception:
            self.addFailure(testname, str(exception))
        elif error_msg:
            self.addFailure(testname, str(error_msg))
        else:
            self.addFailure(testname, '<No exception or environment>')

    def _runTest(self, test, numberOfAssertionFailed=0, prefix='', before=lambda x=None: None, after=lambda x=None: None):
        test.initialize()

        msgPrefix = test.name

        testFullName = prefix + test.name

        if not test.is_method:
            Defaults.curr_test_name = testFullName

        try:
            # Python < 3.11
            test_args = inspect.getargspec(test.target).args
        except:
            test_args = inspect.getfullargspec(test.target).args

        if len(test_args) > 0 and not test.is_method:
            try:
                # env = Env(testName=test.name)
                env = Defaults.env_factory(testName=test.name)
            except Exception as e:
                self.handleFailure(testFullName=testFullName, exception=e, prefix=msgPrefix, testname=test.name)
                return 0

            fn = lambda: test.target(env)
            before_func = lambda: before(env)
            after_func = lambda: after(env)
        else:
            fn = test.target
            before_func = before
            after_func = after

        hasException = False
        try:
            before_func()
            fn()
            passed = True
        except unittest.SkipTest:
            self.printSkip(testFullName)
            return 0
        except TestAssertionFailure:
            if self.args.exit_on_failure:
                self.takeEnvDown(fullShutDown=True)

            # Don't fall-through
            raise
        except Exception as err:
            if self.args.exit_on_failure:
                self.takeEnvDown(fullShutDown=True)
                after_func = lambda x=None: None
                raise

            self.handleFailure(testFullName=testFullName, exception=err, prefix=msgPrefix,
                               testname=test.name, env=self.currEnv)
            hasException = True
            passed = False
        finally:
            after_func()

        numFailed = 0
        if self.currEnv:
            numFailed = self.currEnv.getNumberOfFailedAssertion()
            if numFailed > numberOfAssertionFailed:
                self.handleFailure(testFullName=testFullName, prefix=msgPrefix,
                                   testname=test.name, env=self.currEnv)
                passed = False
        elif not hasException:
            self.addFailure(test.name, '<Environment destroyed>')
            passed = False

        # Handle debugger, if needed
        if self.args.stop_on_failure and not passed:
            if self.args.interactive_debugger:
                while self.currEnv.isUp():
                    time.sleep(1)
            input('press any button to move to the next test')

        if passed:
            self.printPass(testFullName)

        if hasException:
            numFailed += 1 # exception should be counted as failure
        return numFailed

    def printSkip(self, name):
        print('%s:\r\n\t%s' % (Colors.Cyan(name), Colors.Green('[SKIP]')))

    def printFail(self, name):
        print('%s:\r\n\t%s' % (Colors.Cyan(name), Colors.Bred('[FAIL]')))

    def printError(self, name):
        print('%s:\r\n\t%s' % (Colors.Cyan(name), Colors.Bred('[ERROR]')))

    def printPass(self, name):
        print('%s:\r\n\t%s' % (Colors.Cyan(name), Colors.Green('[PASS]')))

    def envScopeGuard(self):
        return EnvScopeGuard(self)

    def killEnvWithSegFault(self):
        if self.currEnv and Defaults.print_verbose_information_on_failure:
            try:
                verboseInfo = {}
                # It is not safe to get the information before dispose, Redis might be stack and will not reply.
                # It will cause us to hand here forever. We will only get the information after dispose, this should be
                # enough as we kill Redis with segfualt which means that it should provide use with all the required details.
                self.stopEnvWithSegFault()
                verboseInfo['after_dispose'] = self.currEnv.getInformationAfterDispose()
                self.currEnv.debugPrint(json.dumps(verboseInfo, indent=2).replace('\\n', '\n'), force=True)
            except Exception as e:
                print('Failed %s' % str(e))
        else:
            self.stopEnvWithSegFault()

    def run_single_test(self, test, on_timeout_func):
        done = 0
        with TestTimeLimit(self.args.test_timeout, on_timeout_func) as timeout_handler:
            with self.envScopeGuard():
                if test.is_class:
                    test.initialize()

                    Defaults.curr_test_name = test.name
                    try:
                        obj = test.create_instance()

                    except unittest.SkipTest:
                        self.printSkip(test.name)
                        return 0

                    except Exception as e:
                        self.printException(e)
                        self.addFailure(test.name + " [__init__]")
                        return 0

                    failures = 0
                    before = getattr(obj, 'setUp', lambda x=None: None)
                    after = getattr(obj, 'tearDown', lambda x=None: None)
                    for subtest in test.get_functions(obj):
                        timeout_handler.reset()
                        failures += self._runTest(subtest, prefix='\t',
                                                numberOfAssertionFailed=failures,
                                                before=before, after=after)
                        done += 1

                else:
                    failures = self._runTest(test)
                    done += 1

                verboseInfo = {}
                if failures > 0 and Defaults.print_verbose_information_on_failure:
                    lastEnv = self.currEnv
                    verboseInfo['before_dispose'] = lastEnv.getInformationBeforeDispose()

        # here the env is down so lets collect more info and print it
        if failures > 0 and Defaults.print_verbose_information_on_failure:
            verboseInfo['after_dispose'] = lastEnv.getInformationAfterDispose()
            lastEnv.debugPrint(json.dumps(verboseInfo, indent=2).replace('\\n', '\n'), force=True)
        return done

    def print_failures(self):
        for group, failures in self.testsFailed.items():
            print('\t' + Colors.Bold(group))
            if not failures:
                print('\t\t' + Colors.Bred('Exception raised during test execution. See logs'))
            for failure in failures:
                print('\t\t' + failure)

    def disable_progress_bar(self):
        return self.args.no_output_catch or self.args.no_progress or not sys.stdout.isatty()

    def progressbar(self, num_elements):
        bar = None
        if not self.disable_progress_bar():
            bar = ProgressBar(max_value=num_elements, redirect_stdout=True)
            for i in range(num_elements):
                bar.update(i)
                yield i
            bar.update(num_elements)
        else:
            yield from range(num_elements)

    def execute(self):
        Env.RTestInstance = self
        if self.args.env_only:
            Defaults.verbose = 2
            # env = Env(testName='manual test env')
            env = Defaults.env_factory(testName='manual test env')
            if self.args.interactive_debugger:
                while env.isUp():
                    time.sleep(1)
            else:
                cmd = MyCmd(env)
                cmd.cmdloop()
            env.stop()
            return
        done = 0
        startTime = time.time()
        if self.args.interactive_debugger and len(self.loader.tests) != 1:
            print(self.tests)
            print(Colors.Bred('only one test can be run on interactive-debugger use -t'))
            sys.exit(1)

        jobs = Queue()
        n_jobs = 0
        for test in self.loader:
            jobs.put(test, block=False)
            n_jobs += 1

        def run_jobs_main_thread(jobs):
            nonlocal done
            bar = self.progressbar(n_jobs)
            for _ in bar:
                try:
                    test = jobs.get(timeout=0.1)
                except Exception as e:
                    break

                def on_timeout():
                    nonlocal done
                    try:
                        done += 1
                        self.killEnvWithSegFault()
                        self.handleFailure(testFullName=test.name, testname=test.name, error_msg=Colors.Bred('Test timeout'))
                        self.print_failures()
                    finally:
                        # we must update the bar anyway to see output
                        bar.__next__()

                done += self.run_single_test(test, on_timeout)

            self.takeEnvDown(fullShutDown=True)

        def run_jobs(jobs, results, summary, port):
            Defaults.port = port
            done = 0
            while True:
                try:
                    test = jobs.get(timeout=0.1)
                except Exception as e:
                    break

                output = io.StringIO()
                with redirect_stdout(output):
                    def on_timeout():
                        nonlocal done
                        try:
                            done += 1
                            self.killEnvWithSegFault()
                            self.handleFailure(testFullName=test.name, testname=test.name, error_msg=Colors.Bred('Test timeout'))
                        except Exception as e:
                            self.handleFailure(testFullName=test.name, testname=test.name, error_msg=Colors.Bred('Exception on timeout function %s' % str(e)))
                        finally:
                            results.put({'test_name': test.name, "output": output.getvalue()}, block=False)
                            summary.put({'done': done, 'failures': self.testsFailed}, block=False)
                            # After we return the processes will be killed, so we must make sure the queues are drained properly.
                            results.close()
                            summary.close()
                            summary.join_thread()
                            results.join_thread()
                    done += self.run_single_test(test, on_timeout)

                results.put({'test_name': test.name, "output": output.getvalue()}, block=False)

            self.takeEnvDown(fullShutDown=True)

            # serialized the results back
            summary.put({'done': done, 'failures': self.testsFailed}, block=False)

        results = Queue()
        summary = Queue()
        if self.parallelism == 1:
            run_jobs_main_thread(jobs)
        else :
            processes = []
            currPort = Defaults.port
            for i in range(self.parallelism):
                p = Process(target=run_jobs, args=(jobs,results,summary,currPort))
                currPort += 30 # safe distance for cluster and replicas
                processes.append(p)
                p.start()
            for _ in self.progressbar(n_jobs):
                while True:
                    # check if we have some lives executors
                    has_live_processor = False
                    for p in processes:
                        if p.is_alive():
                            has_live_processor = True
                            break
                    try:
                        res = results.get(timeout=1)
                        break
                    except Exception as e:
                        if not has_live_processor:
                            raise Exception('Failed to get job result and no more processors is alive')
                output = res['output']
                print('%s' % output, end="")

            for p in processes:
                p.join()

            # join results
            while True:
                try:
                    res = summary.get(timeout=1)
                except Exception as e:
                    break
                done += res['done']
                self.testsFailed.update(res['failures'])

        endTime = time.time()

        print(Colors.Bold('\nTest Took: %d sec' % (endTime - startTime)))
        print(Colors.Bold('Total Tests Run: %d, Total Tests Failed: %d, Total Tests Passed: %d' % (done, self.getFailedTestsCount(), done - self.getFailedTestsCount())))
        if self.testsFailed:
            if self.args.failed_tests_file:
                with open(self.args.failed_tests_file, 'w') as file:
                    for test, _ in self.testsFailed:
                        file.write(test.split(' ')[0] + "\n")

            print(Colors.Bold('Failed Tests Summary:'))
            self.print_failures()
            sys.exit(1)
        else:
            if self.args.failed_tests_file:
                with open(self.args.failed_tests_file, 'w') as file:
                    pass


def main():
    # Avoid "UnicodeEncodeError: 'ascii' codec can't encode character" errors
    sys.stdout = io.open(sys.stdout.fileno(), 'w', encoding='utf8')
    sys.stderr = io.open(sys.stderr.fileno(), 'w', encoding='utf8')
    # Set multiprocessing start method to fork, we have unserializable objects in the env
    set_start_method('fork')
    RLTest().execute()


if __name__ == '__main__':
    main()
