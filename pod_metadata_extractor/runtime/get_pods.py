# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import logging
import os

import boto3
from botocore.client import Config
from utils import create_kube_config_file

from kubernetes import client
from kubernetes import config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HTTP_OK = 200
HTTP_INTERNAL_SERVER_ERROR = 500

DEFAULT_APP_LABEL = "app"

K8S_CLIENT_ROLE_ARN = os.getenv("K8S_CLIENT_ROLE_ARN")
OUTPUT_BUCKET_NAME = os.getenv("OUTPUT_BUCKET_NAME")
CURRENT_ACCOUNT_ID = os.getenv("CURRENT_ACCOUNT_ID")
CLUSTER_NAME = os.getenv("CLUSTER_NAME")

APP_LABEL = os.getenv("APP_LABEL", DEFAULT_APP_LABEL)
AZ_LABEL = "topology.kubernetes.io/zone"

TIME_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

KUBE_CONFIG_FILE_PATH = "/tmp/kubeconfig"

PODS_METADATA_FILENAME = "pods_metadata.csv"

s3_client = boto3.client("s3", config=Config(signature_version="s3v4"))

# Create kubeconfig file on cold-start
logging.info(f"Creating kubeconfig file")
try:
    create_kube_config_file(
        config_file_path=KUBE_CONFIG_FILE_PATH,
        cluster_name="ably-prod",
        k8s_client_role_arn=K8S_CLIENT_ROLE_ARN,
    )
except Exception as exception:
    logging.error(f"There was a problem creating kubeconfig file: {exception}")
    raise exception

logging.info(f"Creating kubeconfig file-ops")
try:
    create_kube_config_file(
        config_file_path=KUBE_CONFIG_FILE_PATH,
        cluster_name="ably-prod-ops",
        k8s_client_role_arn=K8S_CLIENT_ROLE_ARN,
    )
except Exception as exception:
    logging.error(f"There was a problem creating kubeconfig file: {exception}")
    raise exception

logging.info(f"Creating kubeconfig file-hash")
try:
    create_kube_config_file(
        config_file_path=KUBE_CONFIG_FILE_PATH,
        cluster_name="ably-prod-0k5qsh",
        k8s_client_role_arn=K8S_CLIENT_ROLE_ARN,
    )
except Exception as exception:
    logging.error(f"There was a problem creating kubeconfig file: {exception}")
    raise exception


def get_k8s_client(config_file_path: str, context: str) -> client.CoreV1Api:
    config.load_kube_config(config_file=config_file_path, context=context)
    return client.CoreV1Api()

# Create clients for each cluster
v1_default = get_k8s_client(KUBE_CONFIG_FILE_PATH, context="arn:aws:eks:ap-northeast-2:145481888492:cluster/ably-prod")
v1_ops = get_k8s_client(KUBE_CONFIG_FILE_PATH, context="arn:aws:eks:ap-northeast-2:145481888492:cluster/ably-prod-ops")
v1_hash = get_k8s_client(KUBE_CONFIG_FILE_PATH, context="arn:aws:eks:ap-northeast-2:145481888492:cluster/ably-prod-0k5qsh")

def lambda_handler(event, context):
    """
    Handler function that will be excecuted when Lambda Function is invoked
    """

    logging.info(f"Starts extracting pod metadata from cluster: {CLUSTER_NAME}")

    try:
        logging.info(f"Getting EKS nodes metadata")
        nodes_azs = get_nodes_availability_zones(v1_default)

        logging.info(f"Getting EKS pods metadata")
        pods_info = get_pods_info(v1_default, nodes_azs)

    except Exception as exception:
        error_message = f"There was a problem with the requests to the EKS cluster, please verify role mapping in ConfigMap/aws-auth: {exception}"
        logging.error(error_message)
        return {
            "statusCode": HTTP_INTERNAL_SERVER_ERROR,
            "body": error_message,
        }
    
    try:
        logging.info(f"Getting EKS nodes metadata-ops")
        nodes_azs = get_nodes_availability_zones(v1_ops)

        logging.info(f"Getting EKS pods metadata-ops")
        pods_info_ops = get_pods_info(v1_ops, nodes_azs)

    except Exception as exception:
        error_message = f"There was a problem with the requests to the EKS cluster, please verify role mapping in ConfigMap/aws-auth: {exception}"
        logging.error(error_message)
        return {
            "statusCode": HTTP_INTERNAL_SERVER_ERROR,
            "body": error_message,
        }
    
    try:
        logging.info(f"Getting EKS nodes metadata-hash")
        nodes_azs = get_nodes_availability_zones(v1_hash)

        logging.info(f"Getting EKS pods metadata-hash")
        pods_info_hash = get_pods_info(v1_hash, nodes_azs)

    except Exception as exception:
        error_message = f"There was a problem with the requests to the EKS cluster, please verify role mapping in ConfigMap/aws-auth: {exception}"
        logging.error(error_message)
        return {
            "statusCode": HTTP_INTERNAL_SERVER_ERROR,
            "body": error_message,
        }

    try:
        logging.info(f"Ceating local CSV file from pods' metadata")
        logging.info(pods_info)
        logging.info(pods_info_ops)
        logging.info(pods_info_hash)
        
        file_path = create_pods_metadata_csv_file(pods_info + pods_info_ops + pods_info_hash)

    except Exception as exception:
        error_message = f"There was a problem ceating local CSV file from pods' metadata: {exception}"
        logging.error(error_message)
        return {
            "statusCode": HTTP_INTERNAL_SERVER_ERROR,
            "body": error_message,
        }

    try:
        logging.info(f"Uploading pods' metadata to S3 Bucket ({OUTPUT_BUCKET_NAME})")
        upload_file_to_s3(file_path)

    except Exception as exception:
        logging.error(f"There was a problem uploading CSV file to S3: {exception}")

    return {
        "statusCode": HTTP_OK,
        "body": "Pods' metadata successfully uploaded to S3",
    }


def get_nodes_availability_zones(v1: client.CoreV1Api,) -> dict[str, str]:
    """
    Requests EKS nodes metadata and returns the nodes' availability zones
    """
    nodes_azs = {}

    nodes = v1.list_node(watch=False)

    for node in nodes.items:
        nodes_azs[node.metadata.name] = (
            node.metadata.labels[AZ_LABEL]
            if AZ_LABEL in node.metadata.labels
            else "<none>"
        )
    return nodes_azs


def get_pods_info(v1: client.CoreV1Api, nodes_azs: dict[str, str]) -> dict[str, str]:
    """
    Requests pods metadata from EKS
    """
    pods_info = []

    # 첫 번째 레이블 선택자
    pods1 = v1.list_pod_for_all_namespaces(label_selector=APP_LABEL, watch=False)
    
    # 두 번째 레이블 선택자
    pods2 = v1.list_pod_for_all_namespaces(label_selector="app.kubernetes.io/name", watch=False)

    # 두 목록을 결합하고 중복을 제거
    pods = {pod.metadata.name: pod for pod in pods1.items + pods2.items}.values()

    for pod in pods:
        conditions = pod.status.conditions
        
        if not conditions:
            continue
        
        ready_condition = next(filter(lambda cond: getattr(cond, "type", None) == "Ready", conditions), None)
        
        if not ready_condition:
            continue
        
        pod_creation_time = ready_condition.last_transition_time.strftime(
            TIME_DATE_FORMAT
        )            
        app_label = pod.metadata.labels.get(APP_LABEL) or pod.metadata.labels.get("app.kubernetes.io/name", "<none>")

        info = {
            "name": pod.metadata.name,
            "ip": pod.status.pod_ip,
            "app": app_label,
            "creation_time": pod_creation_time,
            "node": pod.spec.node_name,
            "az": nodes_azs.get(pod.spec.node_name, "<none>"),
        }
        pods_info.append(info)   

    return pods_info


def create_pods_metadata_csv_file(pods_info: dict[str, str]) -> str:
    """
    Creates a local /tmp/pods_metadata.csv file before uploading the pods metadata to S3
    """
    file_path = f"/tmp/{PODS_METADATA_FILENAME}"

    pod_header_row = ",".join(["name", "ip", "app", "creation_time", "node", "az"])
    
    pod_data_rows = []
    for info in pods_info:
        if info["ip"] == None:
            continue
        row = ",".join(info.values())
        pod_data_rows.append(row)

    with open(file_path, "w") as f:
        f.write(f"{pod_header_row}\n")
        f.write("\n".join(pod_data_rows))

    return file_path


def upload_file_to_s3(file_path: str) -> None:
    s3_client.upload_file(
        file_path,
        OUTPUT_BUCKET_NAME,
        PODS_METADATA_FILENAME,
        ExtraArgs={"ExpectedBucketOwner": CURRENT_ACCOUNT_ID},
    )
