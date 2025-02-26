import datetime
import math
import random
import time
from pathlib import Path
from typing import Optional

from humanfriendly import parse_timespan, parse_size
import json
import os
import shutil
import stat
import subprocess
from sqlalchemy import desc

from db import session
import model

"""
Specifikace \data v databazi modulu pro "programming":
        "programming": {
            "version": Text, <- default: 1.0
            "default_code": Text,
            "merge_script": Text (path/to/merge/script.py),
            "stdin": Text,
            "args": "[]", <- tento argument je nepovinny
            "timeout": Integer, <- tento argument je nepovinny
            "check_script": Text (path/to/check/script)
        }
"""

MODULE_LIB_PATH = 'data/module_lib/'
EXEC_PATH = '/tmp/box/'
MAX_CONCURRENT_EXEC = 3
BOX_ID_PREFIX: int = 2
assert BOX_ID_PREFIX > 0
STORE_PATH = 'data/exec/'
SOURCE_FILE = 'source'
RESULT_FILE = 'eval.out'

# Default quotas for sandbox.
QUOTA_MEM = "50M"
QUOTA_WALL_TIME = "5s"
QUOTA_BLOCKS = 100
QUOTA_INODES = 100
QUOTA_FILE_SIZE = "50M"
OUTPUT_MAX_LEN = 5000  # in bytes


class ENoFreeBox(Exception):
    pass


class EIsolateError(Exception):
    pass


class ECheckError(Exception):
    pass


class EMergeError(Exception):
    pass


class Reporter(object):
    def __init__(self, initial_value: str = "", max_size: int = 1024**3) -> None:
        """
        Keeps string report.
        Adding a string will add a string into a report attribute
        :param max_size: maximal byte length of the saved string when truncated report is queried, default to 1 GiB
        """
        self.__report_start: str = initial_value
        self.__report_end: str = ''
        self.__max_size: int = max_size
        self.__truncated_length: int = 0

    @property
    def report(self) -> str:
        """
        Gets the full report up to the maximal size without truncation warning
        :return: full report content
        """
        return self.__report_start

    @property
    def report_truncated(self) -> str:
        """
        Gets the report, possibly truncating the content if size is larger than max_size
        :return: full report with maximal size of max_size
        """
        byte_size = len(self.__report_start.encode('utf8'))
        if byte_size < self.__max_size:
            return self.__report_start
        text_truncated = f" [TRUNCATED {self.__truncated_length} CHARACTERS] "
        return self.__report_start[:self.__max_size - len(text_truncated)] + text_truncated + self.__report_end

    def __iadd__(self, other: str) -> "Reporter":
        """
        Appends a string to the report
        :param other: another string to append to the report
        :return: self
        """
        max_new_length = self.__max_size - len(self.__report_start)
        if max_new_length <= 0:
            self.__truncated_length += len(other)

            # we possibly want to keep both the beginning of the output and the end
            # so we strip out the middle part
            if abs(len(self.__report_start) - len(self.__report_end)) > 10:
                self.__report_start = self.__report_start[:min(self.__max_size, len(other))]
                self.__report_end += other[:self.__max_size]
            return self
        self.__truncated_length += max(0, len(other) - max_new_length)
        self.__report_start += other[:max_new_length]
        return self


def to_json(db_dict, user_id, module_id, last_eval):
    code = {
        'default_code': db_dict['programming']['default_code'],
        'code': db_dict['programming']['default_code'],
    }

    # Pick last participant`s code and return it to participant.
    if last_eval is not None:
        submitted = session.query(model.SubmittedCode).\
            filter(model.SubmittedCode.evaluation == last_eval.id).\
            first()

        if submitted is not None:
            code['code'] = submitted.code
            code['last_datetime'] = last_eval.time
            code['last_origin'] = 'evaluation'
    else:
        execution = session.query(model.CodeExecution).\
            filter(model.CodeExecution.module == module_id,
                   model.CodeExecution.user == user_id).\
            order_by(desc(model.CodeExecution.time)).\
            first()

        if execution is not None:
            code['code'] = execution.code
            code['last_datetime'] = execution.time
            code['last_origin'] = 'execution'

    return code


def exec_to_json(ex):
    return {
        'id': ex.id,
        'module': ex.module,
        'user': ex.user,
        'code': ex.code,
        'result': ex.result,
        'time': str(ex.time),
        'report': ex.report,
    }


def evaluate(task, module, user_id, code, eval_id, reporter: Reporter):
    """Evaluate task. Run merge, code, check."""

    prog_info = json.loads(module.data)['programming']
    if ("version" not in prog_info or
            _parse_version(prog_info["version"])[0] < 2):
        reporter += "Unsupported programming version %s\n"
        return {
            'result': 'error',
            'message': 'Opravení této úlohy není webovým systémem '
                       'podporováno.',
        }

    try:
        box_id = init_exec_environment()
    except ENoFreeBox:
        reporter += "Reached limit of concurrent tasks!\n"
        return {
            'result': 'error',
            'message': 'Přesáhnut maximální počet zároveň spuštěných opravení,'
                       ' zkuste to později.'
        }

    check_res = {}
    try:
        try:
            isolate_err = False
            res = _run(prog_info, code, box_id, reporter, user_id,
                       run_type='eval')

            if res["code"] == 0:
                check_res = _check(os.path.join(EXEC_PATH, box_id),
                                   prog_info['check_script'],
                                   os.path.join(EXEC_PATH, box_id, "output"),
                                   reporter, user_id)
            else:
                return {
                    'result': 'nok',
                    'message': 'Tvůj kód se nepodařilo spustit, oprav si '
                               'chyby!',
                    'stdout': res['stdout'],
                }
        except EIsolateError:
            isolate_err = True
            raise
        finally:
            if not isolate_err:
                store_exec(box_id, user_id, module.id,
                           'evaluation\n' + str(eval_id) + '\n')

    finally:
        cleanup_exec_environment(box_id)

    res = {
        'result': 'ok' if res['code'] == 0 and check_res['success'] else 'nok',
        'stdout': res['stdout'],
        'actions': check_res['actions'],
    }

    if 'message' in check_res:
        res['message'] = check_res['message']

    if ('score' in check_res and check_res['score'] <= module.max_points
            and check_res['score'] >= 0):
        res['score'] = check_res['score']
    else:
        res['score'] = module.max_points if res['result'] == 'ok' else 0

    return res


def find_free_box_id() -> Optional[str]:
    """
    limits = prog_info["limits"] if "limits" in prog_info else {}
    Returns is of the first available sandbox directory. Searched for
    non-existing directories in /tmp/box.
    """
    dir_boxes = Path(EXEC_PATH)

    if len(list(
            filter(lambda x: x.name.startswith(f"{BOX_ID_PREFIX}"), dir_boxes.iterdir())
    )) >= MAX_CONCURRENT_EXEC:
        return None

    while True:
        box_name = f"{BOX_ID_PREFIX}{int(((time.time() * 1000 % 10**5) * random.randint(0, 1000))%1000):03d}"
        if not dir_boxes.joinpath(box_name).exists():
            return box_name
        time.sleep(0.001)


def init_exec_environment():
    """Initialize sandbox."""

    # Create directory for sandbox
    if not os.path.exists(EXEC_PATH):
        os.makedirs(EXEC_PATH)

    box_id = find_free_box_id()

    if box_id is None:
        raise ENoFreeBox("Reached limit of concurrent tasks!")

    # Run isolate --init
    p = subprocess.Popen(
        ["isolate", "-b", box_id, "--init"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = p.communicate(timeout=60)
    if p.returncode != 0:
        raise EIsolateError(
            f"Isolate --init for box '{box_id}' returned code ({p.returncode})\n"
            f"---- STDOUT ----\n"
            f"{stdout}\n"
            f"---- STDERR ----\n"
            f"{stderr}"
        )

    return box_id


def cleanup_exec_environment(box_id):
    """Clean-up sandbox data."""

    sandbox_root = os.path.join(EXEC_PATH, box_id)
    if os.path.isdir(sandbox_root):
        p = subprocess.Popen(
            ["isolate", "-b", box_id, "--cleanup"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        p.wait()
        if p.returncode != 0:
            stdout, stderr = p.communicate()
            print('Error cleaning directory %s:\n%s\n%s' % (
                    box_id, stdout.decode('utf-8'), stderr.decode('utf-8')
                )
            )

        try:
            if os.path.isdir(sandbox_root):
                shutil.rmtree(sandbox_root)
        except:
            pass


def code_execution_dir(user_id: int, module_id: int) -> str:
    dst_path = os.path.abspath(os.path.join(STORE_PATH,
                                            "module_" + str(module_id),
                                            "user_" + str(user_id)))
    return dst_path


def store_exec(box_id, user_id, module_id, source):
    """Save execution permanently to STORE_PATH directory."""

    src_path = os.path.abspath(os.path.join(EXEC_PATH, box_id))
    dst_path = code_execution_dir(user_id, module_id)

    # FIXME: can fail sometimes on dir not empty
    if os.path.isdir(dst_path):
        shutil.rmtree(dst_path)

    IGNORE = ["tmp", "root", "etc", "__pycache__", "*.pyc"]
    # FIXME: can fail sometimes when previously launched in near past
    shutil.copytree(src_path, dst_path, ignore=shutil.ignore_patterns(*IGNORE))

    # Write evaluation id so we can recognize it in the future
    with open(os.path.join(dst_path, SOURCE_FILE), 'w') as s:
        s.write(source)


def _parse_version(version):
    v = version.split(".")
    return (int(v[0]), int(v[1]))


def run(module, user_id, code, exec_id, reporter):
    """Manage whole process of running participant`s code."""

    prog_info = json.loads(module.data)['programming']
    if ("version" not in prog_info or
            _parse_version(prog_info["version"])[0] < 2):
        reporter += "Unsupported programming version %s\n"
        return {
            'message': 'Opravení této úlohy není webovým systémem '
                       'podporováno.',
            'result': 'error',
        }

    try:
        box_id = init_exec_environment()
    except ENoFreeBox as e:
        reporter += str(e) + "\n"
        return {
            'message': 'Přesáhnut maximální počet zároveň spuštěných úloh,'
                       ' zkuste to později.',
            'result': 'error',
        }

    try:
        try:
            isolate_err = False
            res = _run(prog_info, code, box_id, reporter, user_id,
                       run_type='exec')
        except EIsolateError:
            isolate_err = True
            raise
        finally:
            if not isolate_err:
                store_exec(box_id, user_id, module.id,
                           'execution\n' + str(exec_id) + '\n')
    finally:
        cleanup_exec_environment(box_id)

    return {
        'stdout': res['stdout'],
        'result': 'ok',
    }


def _run(prog_info, code, box_id, reporter: Reporter, user_id, run_type = 'exec'):
    """
    Run merge and runs the merged file inside of a sandbox. Requires
    initialized sandbox with id \box_id (str). \data is participant`s code.
    This function can throw exceptions, exceptions must be handled.
    """

    # Prepare files with participant`s code
    sandbox_root = os.path.join(EXEC_PATH, box_id)
    raw_code = os.path.join(sandbox_root, 'raw')
    merged_code = os.path.join(sandbox_root, 'box', 'run')

    # Save participant`s 'raw' code
    reporter += "Saving raw code into %s...\n" % (raw_code)
    with open(raw_code, "w") as f:
        f.write(code)

    # Merge participant`s code
    _merge(sandbox_root, prog_info['merge_script'], raw_code, merged_code,
           reporter, user_id, run_type)

    limits = prog_info["limits"] if "limits" in prog_info else {}

    (return_code, output_path, secret_path, stderr_path) = _exec(
        sandbox_root, box_id, "/box/run", os.path.abspath(prog_info['stdin']),
        reporter, limits
    )

    trigger_data = None
    if return_code == 0:
        with open(output_path, 'r') as f:
            output = f.read(OUTPUT_MAX_LEN)

    else:
        with open(output_path, 'r') as output,\
                open(stderr_path, 'r') as stderr:
            output = output.read(OUTPUT_MAX_LEN) + "\n" +\
                     stderr.read(OUTPUT_MAX_LEN)

    if len(output) >= OUTPUT_MAX_LEN:
        output += "\nOutput too long, stripped!\n"

    return {
        'stdout': output,
        'code': return_code,
    }


def _merge(wd, merge_script, code, code_merged, reporter, user_id, run_type):
    """Run merge script."""

    cmd = [
        os.path.abspath(merge_script),
        os.path.abspath(code),
        os.path.abspath(code_merged),
        os.path.abspath(MODULE_LIB_PATH),
        str(user_id),
        run_type,
    ]

    stdout_path = os.path.join(wd, 'merge.stdout')
    stderr_path = os.path.join(wd, 'merge.stderr')

    reporter += 'Merging code to %s (cmd: %s)\n' % (code_merged, " ".join(cmd))
    reporter += ' * stdout: %s\n' % stdout_path
    reporter += ' * stderr: %s\n' % stderr_path

    with open(stdout_path, 'w') as stdout,\
            open(stderr_path, 'w') as stderr:
        p = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, cwd=wd)
        p.wait()

    if p.returncode != 0:
        reporter += '\nError: Merge script exited with nonzero return code!\n'
        reporter += 'Stderr:\n'
        with open(stderr_path, 'r') as stderr:
            reporter += stderr.read()
        raise EMergeError("Merge script exited with nonzero return code!")

    if not os.path.exists(code_merged):
        reporter += '\nError: merge script did not create merged file!\n'
        raise FileNotFoundError("Merge script did not create merged file!")

    # Add executable flag to merged code
    st = os.stat(code_merged)
    os.chmod(code_merged, st.st_mode | stat.S_IEXEC)


def _exec(sandbox_dir, box_id, filename, stdin_path, reporter: Reporter, limits):
    """Execute single file inside a sandbox."""

    stdout_path = os.path.join(sandbox_dir, "stdout")
    stderr_path = os.path.join(sandbox_dir, "stderr")
    output_path = os.path.join(sandbox_dir, "output")
    secret_path = os.path.join(sandbox_dir, "secret")

    if "mem" not in limits:
        limits["mem"] = QUOTA_MEM

    if "total_time" not in limits:
        limits["total_time"] = QUOTA_WALL_TIME

    if "file_size" not in limits:
        limits["file_size"] = QUOTA_FILE_SIZE

    if "blocks" not in limits:
        limits["blocks"] = QUOTA_BLOCKS

    if "inodes" not in limits:
        limits["inodes"] = QUOTA_INODES

    cmd = [
        "isolate",
        "-b",
        box_id,
        "--dir=/etc=" + os.path.join(sandbox_dir, "etc"),
        "--dir=/etc/alternatives=/opt/etc/alternatives",
        "--env=PATH",
        "--env=LANG=en_US.UTF-8",
        "-Mmeta",
        "-m" + str(parse_size(limits["mem"])),
        "-w" + str(parse_timespan(limits["total_time"])),
        "--fsize=" + str(parse_size(limits["file_size"])//1000),
        "-q" + str(limits["blocks"]) + "," + str(limits["inodes"]),
    ]

    if "cpu_time" in limits:
        cmd.append("-t" + str(parse_timespan(limits["cpu_time"])))

    if "stack" in limits:
        cmd.append("-k" + str(parse_size(limits["stack"])//1000))

    if "processes" in limits:
        cmd.append("-p" + str(limits["processes"]))

    if "net" in limits and limits["net"] == "share":
        cmd.append("--share-net")

    cmd += [
        "-c/box",
        "--run",
        filename,
    ]

    # Mock /etc/passwd
    if not os.path.isdir(os.path.join(sandbox_dir, "etc")):
        os.mkdir(os.path.join(sandbox_dir, "etc"))
    with open(os.path.join(sandbox_dir, "etc", "passwd"), 'w') as f:
        f.write("tester:x:" + str(60000 + int(box_id)) + ":0:Tester:/:\n")

    # Create /etc/alternatives directory to allow mount
    if not os.path.isdir(os.path.join(sandbox_dir, "etc", "alternatives")):
        os.mkdir(os.path.join(sandbox_dir, "etc", "alternatives"))

    reporter += 'Running sandbox: %s\n' % (" ".join(cmd))
    reporter += ' * stdout: %s\n' % stdout_path
    reporter += ' * stderr: %s\n' % stderr_path

    with open(stdin_path, 'r') as stdin, open(stdout_path, 'w') as stdout,\
            open(stderr_path, 'w') as stderr:
        start_time = datetime.datetime.now()
        p = subprocess.Popen(cmd, stdin=stdin, stdout=stdout,
                             stderr=stderr, cwd=sandbox_dir)
    p.wait()

    reporter += "Return code: %d\n" % (p.returncode)
    if p.returncode != 0:
        with open(stdout_path, 'r') as stdout:
            reporter += "Stdout: "

            while True:
                data = stdout.read(4096)
                if not data:
                    break
                reporter += data

            reporter += "\n"
        with open(stderr_path, 'r') as stderr:
            reporter += "Stderr: "

            while True:
                data = stderr.read(4096)
                if not data:
                    break
                reporter += data

            reporter += "\n"

        if p.returncode != 1: # 1 = error in sadbox, >1 = isolate error
            raise EIsolateError("Isolate --run returned code " +
                                str(p.returncode))

    # Post process stdout
    ksi_meta_found = False

    with open(stdout_path, 'r') as stdout_in:
        with open(output_path, 'w') as stdout_out:
            with open(secret_path, 'w') as secret_out:
                while True:
                    # read at most 4096 characters at the time
                    # if whole line is read, the string ends with '\n'
                    line = stdout_in.readline(4096)
                    if not line:
                        break

                    if '#KSI_META_OUTPUT_0a859a#' in line:
                        ksi_meta_found = True
                    elif ksi_meta_found or line.strip().startswith('#KSI_'):
                        secret_out.write(line)
                    else:
                        stdout_out.write(line)

    # Post process stderr
    # _parse_stderr(stderr_path, timeout,
    #   datetime.datetime.now()-start_time, heaplimit)

    return (p.returncode, output_path, secret_path, stderr_path)


def _check(sandbox_dir, check_script, sandbox_stdout, reporter: Reporter, user_id):
    """Run check script."""

    cmd = [
        os.path.abspath(check_script),
        os.path.abspath(sandbox_dir),
        os.path.abspath(sandbox_stdout),
        os.path.abspath(MODULE_LIB_PATH),
        str(user_id),
    ]

    stdout_path = os.path.join(sandbox_dir, 'check.stdout')
    stderr_path = os.path.join(sandbox_dir, 'check.stderr')

    reporter += 'Checking output (cmd: %s)\n' % (" ".join(cmd))
    reporter += ' * stdout: %s\n' % stdout_path
    reporter += ' * stderr: %s\n' % stderr_path

    with open(stdout_path, 'w') as stdout, open(stderr_path, 'w') as stderr:
        p = subprocess.Popen(cmd, stdout=stdout, stderr=stderr,
                             cwd=sandbox_dir)
        p.wait()

    res = {
        'success': (p.returncode == 0),
        'actions': []
    }

    with open(stdout_path, 'r') as f:
        for line in f:
            if line.startswith('action '):
                res['actions'].append(line.strip())

    if os.path.getsize(stderr_path) > 0:
        reporter += "Check script returned nonempty stderr:\n"
        with open(stderr_path, 'r') as f:
            while True:
                data = f.read(4096)
                if not data:
                    break
                reporter += data
        raise ECheckError("Check script returned non-empty stderr!")

    # Load results from optional file.
    result_path = os.path.join(sandbox_dir, RESULT_FILE)
    if os.path.isfile(result_path):
        with open(result_path, 'r') as r:
            data = json.loads(r.read())

        if 'message' in data:
            res['message'] = data['message']

        if 'score' in data:
            res['score'] = round(data['score'], 1)

    return res
