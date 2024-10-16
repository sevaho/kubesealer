import os
from textwrap import dedent
import subprocess
from pathlib import Path
from base64 import b64decode, b64encode
from tempfile import NamedTemporaryFile

import click
import questionary
import yaml
from colorama import Fore
from icecream import ic
from yaml.composer import ComposerError

from kubesealer.cluster import Cluster


class Kubeseal:
    def __init__(self, select_context: bool, certificate=None):
        self.detached_mode = False

        self.binary = "kubeseal"

        if certificate is not None:
            click.echo("===> Working in a detached mode")
            self.detached_mode = True
            self.certificate = certificate
        else:
            home_dir = os.path.expanduser("~")
            self.cluster = Cluster(select_context=select_context)
            self.controller_name = self.cluster.get_controller_name()
            self.controller_namespace = self.cluster.get_controller_namespace()
            self.current_context_name = self.cluster.get_context()
            self.namespaces_list = self.cluster.get_all_namespaces()
            version = self.cluster.get_controller_version()

            try:
                self.cluster.ensure_kubeseal_version(version)
                self.binary = f"{home_dir}/bin/kubeseal-{version}"
            except FileNotFoundError:
                click.echo("==> Falling back to the default kubeseal binary")

        self.temp_file = NamedTemporaryFile()

    def _find_sealed_secrets(self, src: str) -> list:
        secrets = []
        for path in Path(src).rglob("*.yaml"):
            secret = self.parse_existing_secret(str(path.absolute()))
            try:
                if secret is not None and secret["kind"] == "SealedSecret":
                    secrets.append(path.absolute())
            except KeyError:
                ...
        return secrets

    def collect_parameters(self) -> dict:
        if self.detached_mode:
            namespace = questionary.text("Provide namespace for the new secret").unsafe_ask()
        else:
            namespace = questionary.select("Select namespace for the new secret",
                                           choices=self.namespaces_list).unsafe_ask()
        secret_type = questionary.select("Select secret type to create",
                                         choices=["generic", "tls", "docker-registry"]).unsafe_ask()
        secret_name = questionary.text("Provide name for the new secret").unsafe_ask()

        return {"namespace": namespace, "type": secret_type, "name": secret_name}

    def create_generic_secret(self):

        document = dedent(f"""
            # Edit the following secret template
            # Do not forget to adjust the namespace and name
                          
            apiVersion: v1
            data:
              foo: bar
            kind: Secret
            metadata:
              name: default
              namespace: default
        """)

        output = click.edit(document, extension=".yaml")

        document = yaml.safe_load(output)
        namespace = document["metadata"]["namespace"]
        secret_name = document["metadata"]["name"]

        for key in document["data"]:
            document["data"][key] = b64encode(document["data"][key].encode()).decode()

        with open(self.temp_file.name, "w") as stream:
            yaml.safe_dump(document, stream=stream)

        click.echo("===> Generating a temporary generic secret yaml file")

        return secret_name

    def create_tls_secret(self, secret_params: dict):
        click.echo("===> Generating a temporary tls secret yaml file")
        command = (
            f"kubectl create secret tls {secret_params['name']} "
            f"--namespace {secret_params['namespace']} --key tls.key --cert tls.crt "
            f"--dry-run=client -o yaml > {self.temp_file.name}"
        )
        ic(command)

        subprocess.call(command, shell=True)

    def create_regcred_secret(self, secret_params: dict):
        click.echo("===> Generating a temporary tls secret yaml file")

        docker_server = questionary.text("Provide docker-server").unsafe_ask()
        docker_username = questionary.text("Provide docker-username").unsafe_ask()
        docker_password = questionary.text("Provide docker-password").unsafe_ask()

        command = (
            f"kubectl create secret docker-registry {secret_params['name']} "
            f"--namespace {secret_params['namespace']} "
            f"--docker-server={docker_server} "
            f"--docker-username={docker_username} "
            f"--docker-password={docker_password} "
            f"--dry-run=client -o yaml > {self.temp_file.name}"
        )
        ic(command)

        subprocess.call(command, shell=True)

    def seal(self, secret_name: str):
        click.echo("===> Sealing generated secret file")
        secret_filename = f"{secret_name}.sealedsecrets.yaml"
        if self.detached_mode:
            command = (
                f"{self.binary} --format=yaml "
                f"--cert={self.certificate} < {self.temp_file.name} "
                f"> {secret_filename}"
            )
        else:
            command = (
                f"{self.binary} --format=yaml "
                f"--context={self.current_context_name} "
                f"--controller-namespace={self.controller_namespace} "
                f"--controller-name={self.controller_name} < {self.temp_file.name} "
                f"> {secret_filename}"
            )
        ic(command)
        subprocess.call(command, shell=True)
        self.append_argo_annotation(filename=secret_filename)
        click.echo("===> Created new secret: {secret_filename}")
        click.echo("===> Done")

    @staticmethod
    def parse_existing_secret(secret_name: str):
        try:
            with open(secret_name, "r") as stream:
                docs = [doc for doc in yaml.safe_load_all(stream) if doc is not None]
                if len(docs) > 1:
                    raise ComposerError("Only single document yaml files are supported")
                return docs[0]
        except FileNotFoundError:
            click.echo("Provided file does not exists. Aborting.")
            exit(1)


    def merge(self, secret_name: str):
        click.echo(f"===> Updating {secret_name}")
        if self.detached_mode:
            command = (
                f"{self.binary} --format=yaml --merge-into {secret_name} "
                f"--cert={self.certificate} < {self.temp_file.name} "
            )
        else:
            command = (
                f"{self.binary} --format=yaml --merge-into {secret_name} "
                f"--context={self.current_context_name} "
                f"--controller-namespace={self.controller_namespace} "
                f"--controller-name={self.controller_name} < {self.temp_file.name}"
            )
        ic(command)
        subprocess.call(command, shell=True)
        self.append_argo_annotation(filename=secret_name)
        click.echo("===> Done")

    def append_argo_annotation(self, filename: str):
        """
        This method is used to append an annotations that will allow
        ArgoCD to process git repository which has SealedSecrets before
        the related controller is deployed in the cluster

        Parameters:
             filename: the filename of the resulting yaml file
        """
        secret = self.parse_existing_secret(filename)

        click.echo("===> Appending ArgoCD related annotations")
        secret["metadata"]["annotations"] = {"argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true"}

        with open(filename, "w") as stream:
            yaml.safe_dump(secret, stream)

    def fetch_certificate(self):
        """
        This method downloads a certificate that can be used in the future
        to encrypt secrets without direct access to the cluster
        """
        click.echo("===> Downloading certificate for kubeseal...")
        command = (
            f"kubeseal --controller-namespace {self.controller_namespace} "
            f"--context={self.current_context_name} "
            f"--controller-name {self.controller_name} --fetch-cert "
            f"> {self.current_context_name}-kubeseal-cert.crt"
        )
        ic(command)
        subprocess.call(command, shell=True)
        click.echo(f"===> Saved to {Fore.CYAN}{self.current_context_name}-kubeseal-cert.crt")

    def reencrypt(self, src: str):
        """
        This method re-encrypts the existing SealedSecret files in a user provided directory
        using the newest encryption certificate

        Parameters:
            src: the directory with SealedSecret files
        """
        for secret in self._find_sealed_secrets(src):
            click.echo(f"Re-encrypting {secret}")
            os.rename(secret, f"{secret}_tmp")
            command = (
                "kubeseal --format=yaml "
                f"--context={self.current_context_name} "
                f"--controller-namespace {self.controller_namespace} "
                f"--controller-name {self.controller_name} "
                f"--re-encrypt < {secret}_tmp > {secret}"
            )
            ic(command)
            subprocess.call(command, shell=True)
            os.remove(f"{secret}_tmp")
            self.append_argo_annotation(secret)

    def decrypt_and_edit(self, file: str):
        """
        Decrypts the SealedSecret

        Parameters:
            file: the SealedSecret file
        """

        with NamedTemporaryFile() as f:
            secret = self.cluster.find_latest_sealed_secrets_controller_certificate()
            command = (
                f"kubectl get secret -n {self.controller_namespace} "
                f"{secret} -o yaml > {f.name}"
            )
            ic(command)
            subprocess.call(command, shell=True)

            command = (
                "kubeseal "
                f"--context={self.current_context_name} "
                f"--controller-namespace {self.controller_namespace} "
                f"--controller-name {self.controller_name} "
                f"< {file} --recovery-unseal --recovery-private-key {f.name} -oyaml"
            )
            ic(command)
            output = subprocess.check_output(command, shell=True)

            docs = [doc for doc in yaml.safe_load_all(output.decode()) if doc is not None]
            if len(docs) > 1:
                raise ComposerError("Only single document yaml files are supported")

            document = docs[0]

            for key in document["data"]:
                document["data"][key] = b64decode(document["data"][key]).decode()

            

            output = click.edit(yaml.safe_dump(document), extension=".yaml")

            # with open(self.temp_file.name, "r") as stream:
            document = yaml.safe_load(output)

            for key in document["data"]:
                document["data"][key] = b64encode(document["data"][key].encode()).decode()

            with open(self.temp_file.name, "w") as stream:
                yaml.safe_dump(document, stream=stream)


    def backup(self):
        """
        This method makes a backup of the latest SealedSecret controllers encryption secret
        """
        secret = self.cluster.find_latest_sealed_secrets_controller_certificate()
        command = (
            f"kubectl get secret -n {self.controller_namespace} "
            f"{secret} -o yaml > {self.current_context_name}-secret-backup.yaml"
        )
        ic(command)
        subprocess.call(command, shell=True)
