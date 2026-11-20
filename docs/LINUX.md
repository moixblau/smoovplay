# SmoovPlay en Linux con AMD (Fedora + Radeon RX 9070 XT)

Port experimental de SmoovPlay a Linux con tarjetas AMD. Igual que en Windows: chiaki-ng recibe el stream del PS5 y,
con el parche del *frame tap*, copia cada frame a memoria compartida; SmoovPlay lo decodifica, genera los frames
intermedios con RIFE y lo muestra a 60, 90 o 120 fps.

> [!WARNING]
> **Sin probar todavía en hardware real.** La memoria compartida entre el parche de chiaki-ng y SmoovPlay está
> probada en Linux (el autotest C ↔ Python pasa byte a byte), pero RIFE en ROCm, VA-API y la ventana todavía no se han
> probado en una tarjeta AMD real. Si algo falla, el log de `run.py` dice qué ruta ha tomado.

## Qué cambia respecto a Windows

| | Windows (original) | Linux + AMD (este port) |
|---|---|---|
| IA (RIFE) | PyTorch CUDA / TensorRT | PyTorch **ROCm** (la misma API `torch.cuda`, vía HIP). Sin TensorRT |
| Memoria compartida | `CreateFileMapping` + evento con nombre | `/dev/shm/chiaki-frametap` + futex (offset 96 de la cabecera) |
| Reloj del tap | `QueryPerformanceCounter` | `CLOCK_MONOTONIC` en ns |
| Decodificación del stream | NVDEC | **VA-API** (Mesa radeonsi); si no hay, CPU |
| GPU → pantalla | interop CUDA-GL | interop **HIP-GL**; si falla, subida desde la CPU (~2 ms) |
| chiaki-ng | build MSYS2 | **Flatpak** con el parche, instalado junto al de Flathub |
| Lanzador | app C# | no hay: `run.py` desde la terminal |
| Captura de ventana, NVIDIA VFG, RTX VSR, presentación DXGI | sí | no (solo Windows/NVIDIA) |

## 1. Requisitos (Fedora)

```bash
sudo dnf install git python3 python3-devel portaudio libva-utils flatpak flatpak-builder
sudo usermod -aG render,video $USER   # ROCm lo necesita; cierra sesión y vuelve a entrar
```

Para decodificar H.264/H.265 por hardware, Fedora necesita el driver VA-API completo de RPM Fusion (el de Fedora no
trae esos códecs):

```bash
sudo dnf swap mesa-va-drivers mesa-va-drivers-freeworld
```

Sin él también funciona: el stream se decodifica en la CPU (unos 10 ms por frame, en su propio hilo).

## 2. SmoovPlay (Python + ROCm)

```bash
git clone -b linux-amd https://github.com/moixblau/smoovplay && cd smoovplay
bash scripts/setup_linux.sh
```

Crea `.venv`, instala PyTorch para ROCm (por defecto el índice `rocm7.2`, un ~30% más rápido que `rocm6.4` en la RX 9070 XT; la
gfx1201 necesita ROCm 6.4 o más nuevo; cámbialo con `ROCM_INDEX=https://download.pytorch.org/whl/rocm7.1`), el resto de dependencias y los
pesos de RIFE, y comprueba que PyTorch ve la tarjeta.

Mide cuánto tarda RIFE en tu tarjeta:

```bash
.venv/bin/python scripts/benchmark.py --models 4.22.lite --res 1080p
```

Como referencia, en Windows una RTX 4070 Ti SUPER tarda ~9 ms por frame intermedio a 1080p. Para 60 fps (1 frame
intermedio por cada real a 30) hace falta que quepa en ~30 ms; para 90 fps (2 intermedios), en ~15 ms cada uno.

## 3. chiaki-ng con el frame tap

```bash
bash chiaki_tap/build_flatpak.sh
```

Descarga chiaki-ng v1.10.1, le aplica el parche y lo construye con el manifiesto Flatpak oficial de chiaki-ng.
Se instala como `io.github.streetpea.Chiaki4deck//frametap`, al lado del chiaki-ng normal de Flathub, y comparte su
configuración (sesión de PSN y consolas registradas). La primera vez tarda 15 a 30 minutos.

## 4. Jugar

En una terminal, chiaki-ng con el tap:

```bash
bash chiaki_tap/run_chiaki_frametap.sh
```

En otra, SmoovPlay:

```bash
.venv/bin/python run.py --chiaki --display
```

Conecta al PS5 en chiaki-ng como siempre. La ventana de SmoovPlay se abre cuando llega el stream. Teclas en la
ventana: **M** abre el menú (frame rate de salida 60/90/120, modelo, escalado, etc.), **U** cambia el escalador,
**F** pantalla completa. Los ajustes se guardan en `~/.config/framegen/settings.json`.

Consejos:

- En chiaki-ng, pon el stream a **1080p y 60 fps**: SmoovPlay espera 60 Hz con repeticiones y detecta solo los
  frames nuevos de un juego a 30.
- Si un juego a 30 fps se ve irregular, pon *Game frame rate* en **30 fps** en el menú (M).
- Prueba primero a 60 fps de salida; sube a 90 solo si el benchmark da tiempo.

## Problemas

- `[run] AMD ROCm (HIP …)` no aparece: PyTorch no es el de ROCm o no ve la tarjeta. Vuelve a ejecutar
  `scripts/setup_linux.sh` y mira el aviso del grupo `render`.
- `vaapi decoding unavailable`: falta `mesa-va-drivers-freeworld`, o el PyAV de pip no trae VA-API. Funciona en CPU.
  Para VA-API con el FFmpeg del sistema (RPM Fusion): `.venv/bin/pip install --no-binary av av`.
- `GPU->screen copy unavailable`: la interop HIP-GL no ha funcionado; se usa la subida por CPU (2 ms más por frame).
- No llegan frames: comprueba que existe `/dev/shm/chiaki-frametap` mientras chiaki-ng está conectado. Si no, el
  Flatpak no se ha lanzado con `CHIAKI_FRAMETAP=1` (usa `run_chiaki_frametap.sh`).
- Si los CUDA graphs dan problemas en ROCm: `run.py --no-graph`.

## Comprobar el tap sin PS5

```bash
.venv/bin/python chiaki_tap/selftest.py                     # el escritor C del parche contra el lector Python
.venv/bin/python -m pytest tests/test_chiaki_tap.py tests/test_tap.py
```
