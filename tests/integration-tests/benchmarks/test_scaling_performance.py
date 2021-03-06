# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
# See the License for the specific language governing permissions and limitations under the License.
import datetime
import json
import logging
import os
from time import sleep

import boto3
import pytest
from retrying import RetryError, retry

from assertpy import assert_that
from remote_command_executor import RemoteCommandExecutor
from tests.common.assertions import assert_no_errors_in_logs
from tests.common.schedulers_common import get_scheduler_commands
from time_utils import minutes, seconds

METRIC_WIDGET_TEMPLATE = """
    {{
        "metrics": [
            [ "ParallelCluster/benchmarking/{cluster_name}", "ComputeNodesCount", {{ "stat": "Maximum", "label": \
"ComputeNodesCount Max" }} ],
            [ "...", {{ "stat": "Minimum", "label": "ComputeNodesCount Min" }} ],
            [ "AWS/AutoScaling", "GroupDesiredCapacity", "AutoScalingGroupName", "{asg_name}", {{ "stat": "Maximum", \
"label": "GroupDesiredCapacity" }} ],
            [ ".", "GroupInServiceInstances", ".", ".", {{ "stat": "Maximum", "label": "GroupInServiceInstances" }} ]
        ],
        "view": "timeSeries",
        "stacked": false,
        "stat": "Maximum",
        "period": 1,
        "title": "{title}",
        "width": 1400,
        "height": 700,
        "start": "{graph_start_time}",
        "end": "{graph_end_time}",
        "annotations": {{
            "horizontal": [
                {{
                    "label": "Scaling Target",
                    "value": {scaling_target}
                }}
            ],
            "vertical": [
                {{
                    "label": "Start Time",
                    "value": "{start_time}"
                }},
                {{
                    "label": "End Time",
                    "value": "{end_time}"
                }}
            ]
        }},
        "yAxis": {{
            "left": {{
                "showUnits": false,
                "label": "Count"
            }},
            "right": {{
                "showUnits": true
            }}
        }}
    }}"""


@pytest.mark.schedulers(["slurm", "sge", "torque"])
@pytest.mark.benchmarks
def test_scaling_performance(region, scheduler, os, instance, pcluster_config_reader, clusters_factory, request):
    """The test runs benchmarks for the scaling logic."""
    benchmarks_max_time = request.config.getoption("benchmarks_max_time")

    benchmark_params = {
        "region": region,
        "scheduler": scheduler,
        "os": os,
        "instance": instance,
        "scaling_target": request.config.getoption("benchmarks_target_capacity"),
        "scaledown_idletime": 2,
        "job_duration": 60,
    }

    cluster_config = pcluster_config_reader(
        scaledown_idletime=benchmark_params["scaledown_idletime"], scaling_target=benchmark_params["scaling_target"]
    )
    cluster = clusters_factory(cluster_config)
    remote_command_executor = RemoteCommandExecutor(cluster)
    scheduler_commands = get_scheduler_commands(scheduler, remote_command_executor)

    _enable_asg_metrics(region, cluster)
    logging.info("Starting benchmark with following parameters: %s", benchmark_params)
    start_time = datetime.datetime.utcnow()
    if scheduler == "sge":
        kwargs = {"slots": _get_instance_vcpus(region, instance) * benchmark_params["scaling_target"]}
    else:
        kwargs = {"nodes": benchmark_params["scaling_target"]}
    result = scheduler_commands.submit_command("sleep {0}".format(benchmark_params["job_duration"]), **kwargs)
    scheduler_commands.assert_job_submitted(result.stdout)
    compute_nodes_time_series, timestamps, end_time = _publish_compute_nodes_metric(
        scheduler_commands,
        max_monitoring_time=minutes(benchmarks_max_time),
        region=region,
        cluster_name=cluster.cfn_name,
    )

    logging.info("Benchmark completed. Producing outputs and performing assertions.")
    benchmark_params["total_time"] = "{0}seconds".format(int((end_time - start_time).total_seconds()))
    _produce_benchmark_metrics_report(
        benchmark_params,
        region,
        cluster.cfn_name,
        cluster.asg,
        start_time.replace(tzinfo=datetime.timezone.utc).isoformat(),
        end_time.replace(tzinfo=datetime.timezone.utc).isoformat(),
        benchmark_params["scaling_target"],
        request,
    )
    assert_that(max(compute_nodes_time_series)).is_equal_to(benchmark_params["scaling_target"])
    assert_that(compute_nodes_time_series[-1]).is_equal_to(0)
    assert_no_errors_in_logs(remote_command_executor, ["/var/log/sqswatcher", "/var/log/jobwatcher"])


def _publish_compute_nodes_metric(scheduler_commands, max_monitoring_time, region, cluster_name):
    logging.info("Monitoring scheduler status and publishing metrics")
    cw_client = boto3.client("cloudwatch", region_name=region)
    compute_nodes_time_series = [0]
    timestamps = [datetime.datetime.utcnow()]

    @retry(
        retry_on_result=lambda _: len(compute_nodes_time_series) == 1 or compute_nodes_time_series[-1] != 0,
        wait_fixed=seconds(20),
        stop_max_delay=max_monitoring_time,
    )
    def _watch_compute_nodes_allocation():
        try:
            compute_nodes = scheduler_commands.compute_nodes_count()
            logging.info("Publishing metric: count={0}".format(compute_nodes))
            cw_client.put_metric_data(
                Namespace="ParallelCluster/benchmarking/{cluster_name}".format(cluster_name=cluster_name),
                MetricData=[{"MetricName": "ComputeNodesCount", "Value": compute_nodes, "Unit": "Count"}],
            )
            # add values only if there is a transition.
            if compute_nodes_time_series[-1] != compute_nodes:
                compute_nodes_time_series.append(compute_nodes)
                timestamps.append(datetime.datetime.utcnow())
        except Exception as e:
            logging.warning("Failed while watching nodes allocation with exception: %s", e)
            raise

    try:
        _watch_compute_nodes_allocation()
    except RetryError:
        # ignoring this error in order to perform assertions on the collected data.
        pass

    end_time = datetime.datetime.utcnow()
    logging.info(
        "Monitoring completed: compute_nodes_time_series [ %s ], timestamps [ %s ]",
        " ".join(map(str, compute_nodes_time_series)),
        " ".join(map(str, timestamps)),
    )
    logging.info("Sleeping for 3 minutes to wait for the metrics to propagate...")
    sleep(180)

    return compute_nodes_time_series, timestamps, end_time


def _enable_asg_metrics(region, cluster):
    logging.info("Enabling ASG metrics for %s", cluster.asg)
    boto3.client("autoscaling", region_name=region).enable_metrics_collection(
        AutoScalingGroupName=cluster.asg,
        Metrics=["GroupDesiredCapacity", "GroupInServiceInstances", "GroupTerminatingInstances"],
        Granularity="1Minute",
    )


def _publish_metric(region, instance, os, scheduler, state, count):
    cw_client = boto3.client("cloudwatch", region_name=region)
    logging.info("Publishing metric: state={0} count={1}".format(state, count))
    cw_client.put_metric_data(
        Namespace="parallelcluster/benchmarking/test_scaling_speed/{region}/{instance}/{os}/{scheduler}".format(
            region=region, instance=instance, os=os, scheduler=scheduler
        ),
        MetricData=[
            {
                "MetricName": "ComputeNodesCount",
                "Dimensions": [{"Name": "state", "Value": state}],
                "Value": count,
                "Unit": "Count",
            }
        ],
    )


def _produce_benchmark_metrics_report(
    benchmark_params, region, cluster_name, asg_name, start_time, end_time, scaling_target, request
):
    title = ", ".join("{0}={1}".format(key, val) for (key, val) in benchmark_params.items())
    graph_start_time = _to_datetime(start_time) - datetime.timedelta(minutes=2)
    graph_end_time = _to_datetime(end_time) + datetime.timedelta(minutes=2)
    scaling_target = scaling_target
    widget_metric = METRIC_WIDGET_TEMPLATE.format(
        cluster_name=cluster_name,
        asg_name=asg_name,
        start_time=start_time,
        end_time=end_time,
        title=title,
        graph_start_time=graph_start_time,
        graph_end_time=graph_end_time,
        scaling_target=scaling_target,
    )
    logging.info(widget_metric)
    cw_client = boto3.client("cloudwatch", region_name=region)
    response = cw_client.get_metric_widget_image(MetricWidget=widget_metric)
    _write_results_to_outdir(request, response["MetricWidgetImage"])


def _to_datetime(timestamp):
    return datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")


def _write_results_to_outdir(request, image_bytes):
    out_dir = request.config.getoption("output_dir")
    os.makedirs("{out_dir}/benchmarks".format(out_dir=out_dir), exist_ok=True)
    graph_dst = "{out_dir}/benchmarks/{test_name}.png".format(
        out_dir=out_dir, test_name=request.node.nodeid.replace("::", "-")
    )
    with open(graph_dst, "wb") as image:
        image.write(image_bytes)


def _get_instance_vcpus(region, instance):
    bucket_name = "{0}-aws-parallelcluster".format(region)
    s3 = boto3.resource("s3", region_name=region)
    instances_file_content = s3.Object(bucket_name, "instances/instances.json").get()["Body"].read()
    instances = json.loads(instances_file_content)
    return int(instances[instance]["vcpus"])
