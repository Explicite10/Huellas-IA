"""Alta y administración de proyectos cliente (se guardan en config/proyectos.json).

    .venv/bin/python scripts/proyectos.py crear "Créditos" --webhook https://api.creditos.com/webhooks/huellas \
        --dominios creditos.com
    .venv/bin/python scripts/proyectos.py listar
    .venv/bin/python scripts/proyectos.py rotar-key prj_xxxxxxxx
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app import proyectos  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", required=True)
    c = sub.add_parser("crear", help="Registrar un proyecto")
    c.add_argument("nombre")
    c.add_argument("--webhook", help="URL por defecto para los avisos de este proyecto")
    c.add_argument("--dominios", nargs="*", default=[],
                   help="Dominios permitidos para url_retorno, webhook e iframe (p. ej. creditos.com)")
    sub.add_parser("listar", help="Ver proyectos")
    r = sub.add_parser("rotar-key", help="Generar una API key nueva (la anterior deja de funcionar)")
    r.add_argument("proyecto_id")
    a = sub.add_parser("desactivar", help="Bloquear un proyecto")
    a.add_argument("proyecto_id")
    args = p.parse_args()

    if args.comando == "crear":
        nuevo = proyectos.crear(args.nombre, args.webhook, args.dominios)
        print(json.dumps(nuevo, indent=2, ensure_ascii=False))
        print("\nGuarda api_key y webhook_secreto en el backend del proyecto: la API key no se vuelve a mostrar.")
    elif args.comando == "listar":
        print(json.dumps(proyectos.listar(), indent=2, ensure_ascii=False))
    elif args.comando == "rotar-key":
        print(json.dumps({"id": args.proyecto_id, "api_key": proyectos.rotar_api_key(args.proyecto_id)}, indent=2))
    elif args.comando == "desactivar":
        proyectos.desactivar(args.proyecto_id)
        print("Proyecto desactivado")


if __name__ == "__main__":
    try:
        main()
    except HTTPException as e:
        sys.exit(f"Error: {e.detail} (en desarrollo local usa HUELLAS_PERMITIR_HTTP=1)")
    except KeyError as e:
        sys.exit(f"Error: {e.args[0]}")
