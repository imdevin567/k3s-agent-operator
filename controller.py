from typing import Tuple

import kopf
import kubernetes.config as k8s_config
import kubernetes.client as k8s_client
import kubernetes.client.exceptions as k8s_exceptions
import logging
import base64
import os
import subprocess
import yaml

try:
    k8s_config.load_kube_config()
except k8s_config.ConfigException:
    k8s_config.load_incluster_config()


# controller logic for SSHCredential resources
@kopf.on.create("k3ao.devinyoung.io", "v1alpha1", "sshcredentials")
@kopf.on.resume("k3ao.devinyoung.io", "v1alpha1", "sshcredentials")
def create_sshcredential(body, **kwargs):
    logging.info("Checking for existence of secret for sshcredential")
    client = k8s_client.CoreV1Api()
    try:
        secret = client.read_namespaced_secret(
            body.metadata.name, body.metadata.namespace
        )
        logging.debug(secret)
    except k8s_exceptions.ApiException as k8sexcept:
        if k8sexcept.status == 404:
            logging.info("Secret not found, creating...")
            client.create_namespaced_secret(
                body.metadata.namespace, __sshcredential_to_secret(body)
            )
        else:
            logging.warn("We don't know how to handle this yet: " + k8sexcept.status)


def __sshcredential_to_secret(sshcredential) -> k8s_client.V1Secret:
    data = {
        "username": sshcredential.spec["username"],
        "sshKeyContents": sshcredential.spec["sshKeyContents"],
    }
    return k8s_client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client.V1ObjectMeta(name=sshcredential.metadata.name),
        data={},
        string_data=data,
    )


# controller logic for Agent resources
@kopf.on.create("k3ao.devinyoung.io", "v1alpha1", "agents")
@kopf.on.resume("k3ao.devinyoung.io", "v1alpha1", "agents")
def create_agent(body, **kwargs):
    client = k8s_client.CoreV1Api()
    (username, keyfile_path) = __setup_ssh_commands(
        body.metadata.namespace, body.spec["sshKeySecretName"]
    )

    # TODO: Implement this
    k3s_is_installed = False
    if not k3s_is_installed:
        # Get master node
        nodes: k8s_client.V1NodeList = client.list_node(
            label_selector="node-role.kubernetes.io/master=true"
        )

        if len(nodes.items) > 0:
            node: k8s_client.V1Node = {}
            __install_k3s_agent(
                server_ip=nodes.items[0].metadata.annotations["k3s.io/external-ip"],
                address=body.spec["address"],
                port=body.spec["port"],
                username=username,
                keyfile=keyfile_path,
            )


@kopf.on.delete("k3ao.devinyoung.io", "v1alpha1", "agents")
def delete_agent(body, **kwargs):
    logging.info("deleting k3s agent")
    (username, keyfile_path) = __setup_ssh_commands(
        body.metadata.namespace, body.spec["sshKeySecretName"]
    )
    __run_remote_command(
        address=body.spec["address"],
        port=body.spec["port"],
        username=username,
        keyfile=keyfile_path,
        command="k3s-agent-uninstall.sh",
    )


def __install_k3s_agent(
    server_ip: str, address: str, port: int, username: str, keyfile: str
):
    kwargs = {
        "address": address,
        "port": port,
        "username": username,
        "keyfile": keyfile,
    }
    # Create k3ao home dir
    __run_remote_command(
        **kwargs,
        command="mkdir -p $HOME/.k3ao",
    )

    # Upload k3s conf file
    __upload_file(
        **kwargs,
        path="$HOME/.k3ao/config.yaml",
        contents=__k3s_conf(address),
    )

    logging.info("installing k3s agent...")

    # Create k3ao home dir
    __run_remote_command(
        **kwargs,
        command=f"curl -sfL https://get.k3s.io | K3S_TOKEN=homeKluster K3S_CONFIG_FILE=$HOME/.k3ao/config.yaml K3S_URL=https://{server_ip}:6443 sh -s",
    )


def __k3s_conf(address: str):
    # "curl -sfL https://get.k3s.io | K3S_TOKEN=homeKluster K3S_CONFIG_FILE=$HOME/.k3ao/config.yaml K3S_URL=https://{server_ip}:6443 sh -s"
    conf = {
        "token": "homeKluster",
        "node-external-ip": address,
        "node-label": [f"k3s.devinyoung.io/agent-ip={address}"],
    }
    return yaml.dump(conf)


def __run_remote_command(
    address: str, port: int, username: str, keyfile: str, command: str
):
    ssh_cmd = f"ssh -i {keyfile} {username}@{address} -p {port} {command}"
    subprocess.run(ssh_cmd.split(" "))


def __upload_file(
    address: str, port: int, username: str, keyfile: str, path: str, contents: str
):
    __run_remote_command(
        address=address,
        port=port,
        username=username,
        keyfile=keyfile,
        command=f'echo "{contents}" > {path}',
    )


def __setup_ssh_commands(namespace: str, secret_name: str) -> Tuple:
    client = k8s_client.CoreV1Api()
    try:
        secret = client.read_namespaced_secret(secret_name, namespace)
        logging.debug(secret)

        keyfile = "/tmp/k3ao-ssh-key"

        # Remove the file first because permissions
        os.remove(keyfile)

        # Write that shit
        with open(keyfile, mode="w") as f:
            f.write(base64.b64decode(secret.data["sshKeyContents"]).decode())

        # SSH is a bitch so permissions need to be set
        os.chmod(keyfile, 0o400)

        return (base64.b64decode(secret.data["username"]).decode(), keyfile)
    except k8s_exceptions.ApiException as k8sexcept:
        if k8sexcept.status == 404:
            logging.warn(
                "SSHCredential must be created prior to the agent! Ignoring..."
            )
        else:
            logging.warn("We don't know how to handle this yet: " + k8sexcept.status)
    return ("", "")
