# Oráculos de regresión

Este directorio conserva implementaciones Python independientes y el controlador histórico usado por las pruebas. No forma parte de los paquetes ni ofrece un launcher o servidor ejecutable. `oracle_engine.py` conserva únicamente el controlador de referencia.

El motor de producción está en `engine/rust`; el protocolo nativo en `engine/native-protocol`. `tools/build_oracles.py` compila la biblioteca ABI de pruebas y la deposita aquí en `runtime/`. `npm run test:engine` prepara esa biblioteca y ejecuta las comprobaciones. Las dependencias Python y su lockfile pertenecen exclusivamente a este proyecto de pruebas.

El perfil público `engine/runtime/bootstrap-profile.json` y los auxiliares PE siguen siendo recursos de producción. No deben eliminarse como parte de la limpieza de Python.
