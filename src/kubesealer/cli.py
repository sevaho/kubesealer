#!/usr/bin/env python
# -*- coding: utf-8 -*-

__version__ = "1.0.0"

import click
import colorama
from icecream import ic

from kubesealer.kubeseal import Kubeseal


def create_new_secret(kubeseal: Kubeseal):
    secret_name = kubeseal.create_generic_secret()

    kubeseal.seal(secret_name=secret_name)


def edit_secret(kubeseal: Kubeseal, file: str):
    kubeseal.decrypt_and_edit(file)
    kubeseal.merge(file)


@click.command(help="Automate the process of sealing secrets for Kubernetes")
@click.argument('file', type=click.Path(), required=False)
@click.option("--debug", required=False, is_flag=True, help="print debug information")
@click.option("--version", "-v", required=False, is_flag=True, help="print version")
@click.option("--select", required=False, is_flag=True, default=False, help="prompt for context select")
def cli(file, debug, version, select):

    if not debug:
        ic.disable()

    if version:
        click.echo(__version__)
        return

    colorama.init(autoreset=True)

    kubeseal = Kubeseal(select_context=select)

    if file:
        edit_secret(kubeseal=kubeseal, file=file)
        return
    else:
        create_new_secret(kubeseal=kubeseal)


if __name__ == "__main__":
    cli()
