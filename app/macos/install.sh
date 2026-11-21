#!/bin/bash
# Instala "BioQL Studio.app" en /Applications dejando el paquete SELLADO y COMPROBADO.
#
#   app/macos/install.sh
#
# Existe porque el 2026-11-21 la app dejó de abrirse y el motivo no estaba en su código: el paquete instalado
# tenía trece ficheros `.pyc` dentro de `Contents/Resources/bioql-studio/studio/__pycache__`. Una firma de macOS
# sella la LISTA de ficheros, así que un fichero AÑADIDO la invalida igual que uno modificado:
#
#     /Applications/BioQL Studio.app: a sealed resource is missing or invalid
#     file added: …/Resources/bioql-studio/studio/__pycache__/mcp_server.cpython-314.pyc
#
# Los escribió el servidor MCP, que corre con `cwd` y `PYTHONPATH` dentro del paquete instalado. Y la instalación
# anterior se hizo a mano, copiando encima: una copia que no BORRA lo viejo deja sobrevivir cualquier cosa que
# alguien dejara allí, y la firma nueva no la cubre. Las dos mitades del defecto se cierran aquí.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="/Applications/BioQL Studio.app"

"$HERE/build.sh"
SRC="$HERE/build/BioQL Studio.app"
[ -d "$SRC" ] || { echo "no se construyó $SRC"; exit 1; }

# Nada de bytecode en lo que se va a firmar. build.sh ya lo excluye; esto lo COMPRUEBA, porque un `--exclude`
# que alguien edite mal no avisa.
if find "$SRC" -name '__pycache__' -o -name '*.pyc' | grep -q .; then
  echo "el paquete construido trae bytecode dentro: eso rompe el sello"; exit 1
fi

# La app corriendo tiene el servicio hijo en 7810. Se le pide salir, y sólo si no sale se insiste.
if pgrep -qx BioQLStudio; then
  osascript -e 'tell application "BioQL Studio" to quit' >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -qx BioQLStudio || break; sleep 1; done
  pgrep -qx BioQLStudio && pkill -x BioQLStudio || true
  sleep 1
fi

# Borrar, no copiar encima. `u+w` primero porque el paquete instalado se deja de sólo lectura (más abajo) y
# `rm` necesita permiso de escritura en el directorio que contiene lo que borra.
if [ -d "$DEST" ]; then chmod -R u+w "$DEST"; rm -rf "$DEST"; fi
ditto "$SRC" "$DEST"

# El tercer cierre: el árbol del servicio, de sólo lectura. Python, al no poder escribir su `.pyc`, sigue
# adelante sin más (ignora el error de escritura por diseño), así que esto no rompe nada y nadie puede volver a
# meter un fichero en el paquete firmado por accidente.
chmod -R a-w "$DEST/Contents/Resources/bioql-studio"

codesign --verify --deep --strict "$DEST"
echo "instalada: $DEST"
codesign -dv "$DEST" 2>&1 | sed -n 's/^Identifier=/  id /p;s/^Signature=/  firma /p'
/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$DEST/Contents/Info.plist" | sed 's/^/  version /'
echo "  sello: $(codesign --verify --strict "$DEST" 2>&1 || echo roto)valido"
