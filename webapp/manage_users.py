#!/usr/bin/env python3
"""
Script para crear al usuario ADMINISTRADOR principal (el unico, vos) del
panel de Bot Familiar.

Ejecutalo VOS MISMO en tu propia terminal (no lo pegues en un chat) para
que la contrasena no quede visible en ningun historial:

    python webapp/manage_users.py --admin miguel_p miguel@gmail.com

Pide la contrasena de forma oculta (no se muestra en pantalla) y guarda
solo su hash en data/users.db. El resto de los familiares se registran
ELLOS MISMOS desde /registro -- este script es solo para el arranque
inicial del admin.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.auth import create_admin_user


def main():
    parser = argparse.ArgumentParser(description="Crea al usuario administrador principal de Bot Familiar")
    parser.add_argument("username", help="Nombre de usuario del administrador")
    parser.add_argument("email", help="Correo del administrador")
    parser.add_argument("--admin", action="store_true", help="(por compatibilidad, siempre crea el admin)")
    args = parser.parse_args()

    password = getpass.getpass("Nueva contrasena: ")
    confirm = getpass.getpass("Confirma la contrasena: ")
    if password != confirm:
        print("Las contrasenas no coinciden. No se ha guardado nada.")
        sys.exit(1)
    if len(password) < 6:
        print("La contrasena debe tener al menos 6 caracteres.")
        sys.exit(1)

    create_admin_user(args.username, args.email, password)
    print(f"Usuario administrador '{args.username}' creado/actualizado. Ya podes entrar con ese usuario y contrasena.")


if __name__ == "__main__":
    main()
