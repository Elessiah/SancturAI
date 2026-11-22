#!/bin/sh
# Lanceur de la CLI (`sancturai …`) dans l'image : le projet n'y est pas installé, app.cli est importé via PYTHONPATH.
exec python -c "from app.cli import main; main()" "$@"
