# Copyright 2018 Xiaomi, Inc.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import filelock
import hashlib
import os
import re
import sh
import time
import urllib

from aibench.proto import base_pb2
from aibench.proto import aibench_pb2
from aibench.python.utils.common import aibench_check
from google.protobuf import text_format


class AIBenchKeyword(object):
    device_type = 'device_type'
    executor = 'executor'
    model_name = 'model_name'
    quantize = 'quantize'


def strip_invalid_utf8(str):
    return sh.iconv(str, "-c", "-t", "UTF-8")


def split_stdout(stdout_str):
    stdout_str = strip_invalid_utf8(stdout_str)
    # Filter out last empty line
    return [l.strip() for l in stdout_str.split('\n') if len(l.strip()) > 0]


def make_output_processor(buff):
    def process_output(line):
        print(line.rstrip())
        buff.append(line)

    return process_output


def device_lock_path(serialno):
    return "/tmp/device-lock-%s" % serialno


def device_lock(serialno, timeout=3600):
    return filelock.FileLock(device_lock_path(serialno), timeout=timeout)


def adb_devices():
    serialnos = []
    p = re.compile(r'(\w+)\s+device')
    for line in split_stdout(sh.adb("devices")):
        m = p.match(line)
        if m:
            serialnos.append(m.group(1))

    return serialnos


def adb_getprop_by_serialno(serialno):
    outputs = sh.adb("-s", serialno, "shell", "getprop")
    raw_props = split_stdout(outputs)
    props = {}
    p = re.compile(r'\[(.+)\]: \[(.+)\]')
    for raw_prop in raw_props:
        m = p.match(raw_prop)
        if m:
            props[m.group(1)] = m.group(2)
    return props


def adb_supported_abis(serialno):
    props = adb_getprop_by_serialno(serialno)
    abilist_str = props["ro.product.cpu.abilist"]
    abis = [abi.strip() for abi in abilist_str.split(',')]
    return abis


def file_checksum(fname):
    hash_func = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_func.update(chunk)
    return hash_func.hexdigest()


def adb_push_file(src_file, dst_dir, serialno):
    if not os.path.isfile(src_file):
        print("Not file, skip pushing " + src_file)
        return
    src_checksum = file_checksum(src_file)
    dst_file = os.path.join(dst_dir, os.path.basename(src_file))
    stdout_buff = []
    try:
        sh.adb("-s", serialno, "shell", "md5sum", dst_file,
               _out=lambda line: stdout_buff.append(line))
    except sh.ErrorReturnCode_1:
        print("Push %s to %s" % (src_file, dst_dir))
        sh.adb("-s", serialno, "push", src_file, dst_dir)
    else:
        dst_checksum = stdout_buff[0].split()[0]
        if src_checksum == dst_checksum:
            print("Equal checksum with %s and %s" % (src_file, dst_file))
        else:
            print("Push %s to %s" % (src_file, dst_dir))
            sh.adb("-s", serialno, "push", src_file, dst_dir)


def adb_push(src_path, dst_dir, serialno):
    if os.path.isdir(src_path):
        for src_file in os.listdir(src_path):
            adb_push_file(os.path.join(src_path, src_file), dst_dir, serialno)
    else:
        adb_push_file(src_path, dst_dir, serialno)


def adb_pull(src_path, dst_path, serialno):
    print("Pull %s to %s" % (src_path, dst_path))
    try:
        sh.adb("-s", serialno, "pull", src_path, dst_path)
    except Exception as e:
        print("Error msg: %s" % e.stderr)


def get_soc_serialnos_map():
    serialnos = adb_devices()
    soc_serialnos_map = {}
    for serialno in serialnos:
        props = adb_getprop_by_serialno(serialno)
        soc_serialnos_map.setdefault(props["ro.board.platform"], []) \
            .append(serialno)

    return soc_serialnos_map


def get_target_socs_serialnos(target_socs=None):
    soc_serialnos_map = get_soc_serialnos_map()
    serialnos = []
    if target_socs is None:
        target_socs = soc_serialnos_map.keys()
    for target_soc in target_socs:
        serialnos.extend(soc_serialnos_map[target_soc])
    return serialnos


def download_file(configs, filename, output_dir):
    file_path = output_dir + "/" + filename
    url = configs[filename]
    checksum = configs[filename + "_md5_checksum"]
    if not os.path.exists(file_path) or file_checksum(file_path) != checksum:
        print("downloading %s..." % filename)
        urllib.urlretrieve(url, file_path)
    if file_checksum(file_path) != checksum:
        print("file %s md5 checksum not match" % filename)
        exit(1)
    return file_path


def get_tflite(configs, output_dir):
    file_path = download_file(configs, "tensorflow-1.10.1.zip", output_dir)
    sh.unzip("-o", file_path, "-d", "third_party/tflite")


def bazel_build(serialno, target, abi, executors, device_types):
    print("* Build %s with ABI %s" % (target, abi))
    if abi == "host":
        bazel_args = (
            "build",
            target,
        )
    else:
        bazel_args = (
            "build",
            target,
            "--config",
            "android",
            "--cpu=%s" % abi,
            "--action_env=ANDROID_NDK_HOME=%s"
            % os.environ["ANDROID_NDK_HOME"],
        )
    for executor in executors:
        bazel_args += ("--define", "%s=true"
                       % base_pb2.ExecutorType.Name(executor).lower())
    bazel_args += ("--define", "neon=true")
    bazel_args += ("--define", "openmp=true")
    bazel_args += ("--define", "opencl=true")
    bazel_args += ("--define", "quantize=true")

    if base_pb2.DSP in device_types and abi == "armeabi-v7a":
        with device_lock(serialno):
            try:
                output = sh.adb("-s", serialno, "shell",
                                "ls /system/lib/libcdsprpc.so")
            except sh.ErrorReturnCode_1:
                print("/system/lib/libcdsprpc.so does not exists! Skip DSP.")
            else:
                if "No such file or directory" in output:
                    print("/system/lib/libcdsprpc.so does not exists! Skip DSP.")  # noqa
                else:
                    bazel_args += ("--define", "dsp=true")
                    bazel_args += ("--define", "hexagon=true")
    sh.bazel(
        _fg=True,
        *bazel_args)
    print("Build done!\n")


def bazel_target_to_bin(target):
    # change //aibench/a/b:c to bazel-bin/aibench/a/b/c
    prefix, bin_name = target.split(':')
    prefix = prefix.replace('//', '/')
    if prefix.startswith('/'):
        prefix = prefix[1:]
    host_bin_path = "bazel-bin/%s" % prefix
    return host_bin_path, bin_name


def prepare_device_env(serialno, abi, device_bin_path, executors):
    opencv_lib_path = ""
    if abi == "armeabi-v7a":
        opencv_lib_path = "bazel-mobile-ai-bench/external/opencv/sdk/native/libs/armeabi-v7a/libopencv_java3.so"  # noqa
    elif abi == "arm64-v8a":
        opencv_lib_path = "bazel-mobile-ai-bench/external/opencv/sdk/native/libs/arm64-v8a/libopencv_java3.so"  # noqa
    if opencv_lib_path:
        adb_push(opencv_lib_path, device_bin_path, serialno)
    # for snpe
    if base_pb2.SNPE in executors:
        snpe_lib_path = ""
        if abi == "armeabi-v7a":
            snpe_lib_path = \
                "bazel-mobile-ai-bench/external/snpe/lib/arm-android-gcc4.9"
        elif abi == "arm64-v8a":
            snpe_lib_path = \
                "bazel-mobile-ai-bench/external/snpe/lib/aarch64-android-gcc4.9"  # noqa

        if snpe_lib_path:
            adb_push(snpe_lib_path, device_bin_path, serialno)
            libgnustl_path = os.environ["ANDROID_NDK_HOME"] + \
                "/sources/cxx-stl/gnu-libstdc++/4.9/libs/%s/" \
                "libgnustl_shared.so" % abi
            adb_push(libgnustl_path, device_bin_path, serialno)

        adb_push("bazel-mobile-ai-bench/external/snpe/lib/dsp",
                 device_bin_path, serialno)

    # for mace
    if base_pb2.MACE in executors and abi == "armeabi-v7a":
        adb_push("third_party/mace/nnlib/libhexagon_controller.so",  # noqa
                 device_bin_path, serialno)

    # for tflite
    if base_pb2.TFLITE in executors:
        tflite_lib_path = ""
        if abi == "armeabi-v7a":
            tflite_lib_path = \
               "third_party/tflite/tensorflow/contrib/lite/" + \
               "lib/armeabi-v7a/libtensorflowLite.so"
        elif abi == "arm64-v8a":
            tflite_lib_path = \
               "third_party/tflite/tensorflow/contrib/lite/" + \
               "lib/arm64-v8a/libtensorflowLite.so"
        if tflite_lib_path:
            adb_push(tflite_lib_path, device_bin_path, serialno)


def get_model_file(file_path, checksum, output_dir, push_list):
    filename = file_path.split('/')[-1]
    if file_path.startswith("http"):
        local_file_path = output_dir + '/' + filename
        if not os.path.exists(local_file_path) \
                or file_checksum(local_file_path) != checksum:
            print("downloading %s..." % filename)
            urllib.urlretrieve(file_path, local_file_path)
        aibench_check(file_checksum(local_file_path) == checksum,
                      "file %s md5 checksum not match" % filename)
    else:
        local_file_path = file_path
        aibench_check(file_checksum(local_file_path) == checksum,
                      "file %s md5 checksum not match" % filename)

    push_list.append(local_file_path)


def get_model(model_info, output_dir, push_list):
    get_model_file(model_info.model_path, model_info.model_checksum,
                   output_dir, push_list)

    if model_info.weight_path != "":
        get_model_file(model_info.weight_path, model_info.weight_checksum,
                       output_dir, push_list)


def get_proto(push_list, output_dir):
    bench_factory = aibench_pb2.BenchFactory()
    model_factory = aibench_pb2.ModelFactory()
    try:
        with open("aibench/proto/benchmark.meta", "rb") as fin:
            file_content = fin.read()
            text_format.Parse(file_content, bench_factory)
            filepath = output_dir + "/benchmark.pb"
            with open(filepath, "wb") as fout:
                fout.write(bench_factory.SerializeToString())
                push_list.append(filepath)
        with open("aibench/proto/model.meta", "rb") as fin:
            file_content = fin.read()
            text_format.Parse(file_content, model_factory)
            filepath = output_dir + "/model.pb"
            with open(filepath, "wb") as fout:
                fout.write(model_factory.SerializeToString())
                push_list.append(filepath)
    except text_format.ParseError as e:
        raise IOError("Cannot parse file.", e)

    return bench_factory, model_factory


def prepare_all_models(executors, model_names, device_types, output_dir):
    push_list = []
    bench_factory, model_factory = get_proto(push_list, output_dir)

    executors = executors.split(',') \
        if executors != "all" else base_pb2.ExecutorType.keys()
    executors = [base_pb2.ExecutorType.Value(e) for e in executors]
    model_names = model_names.split(',') \
        if model_names != "all" else base_pb2.ModelName.keys()
    model_names = [base_pb2.ModelName.Value(m) for m in model_names]
    device_types = device_types.split(',') \
        if device_types != "all" else base_pb2.DeviceType.keys()
    device_types = [base_pb2.DeviceType.Value(d) for d in device_types]

    model_infos = []
    for benchmark in bench_factory.benchmarks:
        if benchmark.executor not in executors:
            continue
        for model in benchmark.models:
            if model.model_name not in model_names:
                continue
            model_info = {
                AIBenchKeyword.executor: benchmark.executor,
                AIBenchKeyword.model_name: model.model_name,
                AIBenchKeyword.quantize: model.quantize,
            }
            downloaded = False
            for device in model.devices:
                if device not in device_types:
                    continue
                if not downloaded:
                    get_model(model, output_dir, push_list)
                    downloaded = True
                info = copy.deepcopy(model_info)
                info[AIBenchKeyword.device_type] = device
                model_infos.append(info)
    return executors, device_types, push_list, model_infos


def push_all_models(serialno, device_bin_path, push_list):
    for path in push_list:
        adb_push(path, device_bin_path, serialno)


def prepare_datasets(configs, output_dir, input_dir):
    if input_dir.startswith("http"):
        file_path = download_file(configs, "imagenet_less.zip", output_dir)
        sh.unzip("-o", file_path, "-d", output_dir)
        return output_dir + "/imagenet_less"
    else:
        return input_dir


def push_precision_files(serialno, device_bin_path, input_dir):
    sh.adb("-s", serialno, "shell", "mkdir -p %s" % device_bin_path)
    adb_push("aibench/benchmark/imagenet/imagenet_blacklist.txt",
             device_bin_path, serialno)
    adb_push("aibench/benchmark/imagenet/imagenet_groundtruth_labels.txt",
             device_bin_path, serialno)
    adb_push("aibench/benchmark/imagenet/mobilenet_model_labels.txt",
             device_bin_path, serialno)
    if input_dir != "":
        imagenet_input_path = device_bin_path + "/inputs/"
        print("Pushing images from %s to %s ..."
              % (input_dir, imagenet_input_path))
        sh.adb("-s", serialno, "shell", "mkdir -p %s" % imagenet_input_path)
        sh.adb("-s", serialno, "push", input_dir, imagenet_input_path)
        base_dir = os.path.basename(input_dir) \
            if input_dir[-1] != '/' else os.path.basename(input_dir[:-1])
        sh.adb("-s", serialno, "shell", "mv %s/* %s"
               % (imagenet_input_path + base_dir, imagenet_input_path))


def get_cpu_mask(serialno):
    freq_list = []
    cpu_id = 0
    cpu_mask = ''
    while True:
        try:
            freq_list.append(
                int(sh.adb("-s", serialno, "shell",
                           "cat /sys/devices/system/cpu/cpu%d"
                           "/cpufreq/cpuinfo_max_freq" % cpu_id)))
        except (ValueError, sh.ErrorReturnCode_1):
            break
        else:
            cpu_id += 1
    for freq in freq_list:
        cpu_mask = '1' + cpu_mask if freq == max(freq_list) else '0' + cpu_mask
    return str(hex(int(cpu_mask, 2)))[2:]


def adb_run(abi,
            serialno,
            host_bin_path,
            bin_name,
            benchmark_option,
            input_dir,
            run_interval,
            num_threads,
            max_time_per_lock,
            push_list,
            benchmark_list,
            executors,
            device_bin_path,
            output_dir,
            ):
    sh.adb("-s", serialno, "shell", "rm -rf %s"
           % os.path.join(device_bin_path, "result.txt"))
    props = adb_getprop_by_serialno(serialno)
    product_model = props["ro.product.model"]
    target_soc = props["ro.board.platform"]

    i = 0
    while i < len(benchmark_list):
        print(
            "============================================================="
        )
        print("Trying to lock device %s" % serialno)
        with device_lock(serialno):
            start_time = time.time()
            print("Run on device: %s, %s, %s" %
                  (serialno, product_model, target_soc))
            try:
                sh.bash("tools/power.sh",
                        serialno, props["ro.board.platform"],
                        _fg=True)
            except Exception, e:
                print("Config power exception %s" % str(e))

            sh.adb("-s", serialno, "shell", "mkdir -p %s"
                   % device_bin_path)
            sh.adb("-s", serialno, "shell", "rm -rf %s"
                   % os.path.join(device_bin_path, "interior"))
            sh.adb("-s", serialno, "shell", "mkdir %s"
                   % os.path.join(device_bin_path, "interior"))

            prepare_device_env(serialno, abi, device_bin_path, executors)
            push_all_models(serialno, device_bin_path, push_list)
            if benchmark_option == base_pb2.Precision:
                push_precision_files(serialno, device_bin_path, input_dir)

            host_bin_full_path = "%s/%s" % (host_bin_path, bin_name)
            device_bin_full_path = "%s/%s" % (device_bin_path, bin_name)
            adb_push(host_bin_full_path, device_bin_path, serialno)
            print("Run %s" % device_bin_full_path)

            cpu_mask = get_cpu_mask(serialno)
            cmd = "cd %s; ADSP_LIBRARY_PATH='.;/system/lib/rfsa/adsp;" \
                  "/system/vendor/lib/rfsa/adsp;/dsp';" \
                  " LD_LIBRARY_PATH=. taskset " \
                  % device_bin_path + cpu_mask + " ./model_benchmark"

            elapse_minutes = 0  # run at least one model
            while elapse_minutes < max_time_per_lock \
                    and i < len(benchmark_list):
                item = benchmark_list[i]
                i += 1
                print(
                    base_pb2.ExecutorType.Name(item[AIBenchKeyword.executor]),
                    base_pb2.ModelName.Name(item[AIBenchKeyword.model_name]),
                    base_pb2.DeviceType.Name(
                        item[AIBenchKeyword.device_type]),
                    "Quantized" if item[AIBenchKeyword.quantize] else "Float")
                args = [
                    "--run_interval=%d" % run_interval,
                    "--num_threads=%d " % num_threads,
                    "--benchmark_option=%s" % benchmark_option,
                    "--executor=%d" % item[AIBenchKeyword.executor],
                    "--device_type=%d" % item[AIBenchKeyword.device_type],
                    "--model_name=%d" % item[AIBenchKeyword.model_name],
                    "--quantize=%s" % item[AIBenchKeyword.quantize],
                    ]
                args = ' '.join(args)
                sh.adb(
                    "-s",
                    serialno,
                    "shell",
                    "%s %s" % (cmd, args),
                    _fg=True)
                elapse_minutes = (time.time() - start_time) / 60
            print("Elapse time: %f minutes." % elapse_minutes)
        # Sleep awhile so that other pipelines can get the device lock.
        time.sleep(run_interval)

    src_path = device_bin_path + "/result.txt"
    dest_path = output_dir + "/" + product_model + "_" + target_soc + "_" \
        + abi + "_" + "result.txt"
    adb_pull(src_path, dest_path, serialno)

    return dest_path
