# Vozi

Dictado por voz **local** para Linux: pulsas **F8**, hablas, pulsas **F8** (o **F9**) otra vez y el texto
queda en el portapapeles, o se pega solo en la ventana activa. Funciona con el modelo de reconocimiento de voz
Whisper (vía [faster-whisper](https://github.com/SYSTRAN/faster-whisper)) en tu computadora, sin enviar audio a internet.

Pensado para Fedora con GNOME en Wayland, pero funciona en cualquier distro con PipeWire.

## Qué lo hace rápido y ligero

- **Sin usarlo no hace nada:** el servicio queda dormido esperando en un socket.
- **El micrófono se abre solo mientras grabas** (`pw-record`).
- **Atajo nativo de GNOME:** responde al instante, sin permisos de root.
- **El texto llega casi en cuanto terminas de hablar**, aunque el dictado sea largo.

Dos técnicas lo hacen posible:

1. **Encoder de longitud dinámica.** Whisper siempre procesa una ventana de 30 s aunque hables 4 s, y el encoder es
   casi todo el costo. Aquí se le pasa solo el audio con voz (tras quitar silencios con VAD)
   más 1 s de margen: entre 5 y 10 veces menos trabajo. Si el resultado parece una alucinación,
   se repite con la ventana completa.
2. **Transcripción anticipada.** Mientras sigues hablando, cada vez que haces una pausa natural lo ya dictado
   se transcribe en segundo plano. Al terminar solo queda procesar la última frase, así que un dictado largo
   responde igual de rápido que uno corto.

Además: el modelo se carga una vez y se queda en memoria (con una inferencia de calentamiento), no se consulta
Hugging Face en cada arranque y se devuelve al sistema la memoria temporal de cada inferencia.

## Instalación

```bash
git clone https://github.com/diegoZN1/vozi.git
cd vozi
./install.sh
```

El instalador:
- instala con `dnf` lo que falte (`pipewire-utils`, `wl-clipboard`, `libnotify`, `uv`);
- instala `vozi` en `~/.local/bin` con `uv` (aislado; no toca el Python del sistema);
- descarga el modelo la primera vez (~480 MB, en `~/.cache/huggingface`);
- crea el servicio de usuario `vozi.service`, que arranca con tu sesión;
- añade los atajos **F8** (grabar/terminar) y **F9** (terminar) en GNOME, sin tocar tus otros atajos.

Otras teclas: `./install.sh --key '<Super>h' --stop-key ''` (formato de GNOME; `''` = sin atajo).
También puedes cambiarlas en *Configuración → Teclado → Atajos personalizados*.

### Pegado automático (opcional)

Por defecto el texto se copia al portapapeles. Para que además se pegue solo donde está el cursor:

```bash
vozi setup-paste
```

En Wayland una app no puede enviar teclas a otras ventanas. Vozi crea un teclado virtual (`/dev/uinput`)
que pulsa **Shift+Insert**, una combinación que pega en navegadores, editores y también en terminales.
Para eso se instala una regla udev que da a tu sesión acceso a `/dev/uinput` (pide tu contraseña una vez).
Ten en cuenta que ese permiso deja que cualquier programa de tu sesión simule teclas, igual que `ydotool`.
Para quitarlo: `vozi setup-paste --off`.

## Uso

| Acción | Cómo |
|---|---|
| Empezar a dictar | **F8**: suena una burbuja que sube y GNOME muestra el ícono del micrófono |
| Terminar | **F8** o **F9**: suena una burbuja que baja mientras se transcribe |
| Texto listo | suena un doble *pop*: ya puedes pegar con Ctrl+V |
| Cancelar | `vozi cancel` |
| Ver estado | `vozi status` |
| Último texto | `vozi last` |
| Elegir micrófono | `vozi devices` y luego `vozi config input_device <nombre>` |
| Registro en vivo | `vozi logs` |
| Probar con un archivo | `vozi transcribe audio.wav` |

Sin configurar nada usa el micrófono predeterminado de GNOME (*Configuración → Sonido → Entrada*).

### Sonidos

Hay tres estilos, todos sintetizados en el momento (sin archivos de audio):

| Estilo | Cómo suena |
|---|---|
| `burbuja` (por defecto) | burbujas suaves que suben y bajan, como en una app de mensajería |
| `moderno` | notas de vidrio en quintas con un poco de reverb, como los sonidos de un teléfono actual |
| `clasico` | pitidos simples |

Escúchalos con `vozi sounds` y elige uno con `vozi config sound_theme moderno`.
Para silenciarlos: `vozi config sounds false`.

## Configuración

`~/.config/vozi/config.toml`. Para verla: `vozi config`. Para cambiar una opción (se aplica sola):
`vozi config <opción> <valor>`. Si editas el archivo a mano, después ejecuta `vozi restart`.

| Opción | Por defecto | |
|---|---|---|
| `model` | `"small"` | `"large-v3-turbo"` es más preciso, pero ~3 veces más lento y usa ~2.5 GB de RAM |
| `language` | `"es"` | `""` = detección automática |
| `beam_size` | `1` | `5` es un poco más preciso y algo más lento |
| `initial_prompt` | frase con puntuación | orienta el estilo; añade aquí nombres propios o términos técnicos |
| `threads` | `0` | `0` = la mitad de los hilos del procesador (lo más rápido en CPUs híbridas) |
| `unload_after_min` | `0` | libera la RAM tras N minutos sin uso; se recarga sola mientras hablas |
| `input_device` | `""` | micrófono de PipeWire |
| `paste` / `paste_keys` | `false` / `"shift+insert"` | pegado automático |
| `sounds` / `sound_theme` | `true` / `"burbuja"` | sonidos al empezar, al terminar y con el texto listo |
| `notifications` | `true` | notificaciones de escritorio |

## Solución de problemas

- **F8 no hace nada:** `vozi status`. Si el servicio no corre: `systemctl --user restart vozi`
  y revisa `vozi logs`. Si `status` funciona, revisa el atajo en *Configuración → Teclado*. En algunos
  portátiles las teclas F hacen de teclas multimedia: prueba con Fn+F8 o elige otra tecla.
- **Graba silencio:** comprueba el micrófono con `vozi devices` y el nivel de entrada en GNOME.
- **No pega en una app concreta:** prueba con `vozi config paste_keys ctrl+v`. El texto siempre queda en el portapapeles.

## Desinstalar

```bash
vozi uninstall && uv tool uninstall vozi
rm -rf ~/.config/vozi ~/.cache/huggingface/hub/models--Systran--faster-whisper-small   # opcional
```

## Licencia

[MIT](LICENSE): puedes usar, modificar y redistribuir el código, incluso con fines comerciales, conservando el
aviso de copyright. Se ofrece sin garantía.

Componentes de terceros, todos con licencias permisivas:

| Componente | Licencia |
|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) y [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| Modelos [Whisper](https://github.com/openai/whisper) de OpenAI (convertidos por SYSTRAN) | MIT |
| [Silero VAD](https://github.com/snakers4/silero-vad) (incluido en faster-whisper) | MIT |
| ONNX Runtime | MIT |
| NumPy, PyAV | BSD |
| tokenizers, huggingface_hub | Apache-2.0 |

Vozi no está afiliado con OpenAI ni respaldado por OpenAI. "Whisper" se menciona solo para indicar el modelo
de reconocimiento de voz que utiliza.

## Estructura

```
src/vozi/
  cli.py       órdenes; las del atajo solo importan `socket` para responder en milisegundos
  daemon.py    servicio: socket Unix, estados y transcripción anticipada
  engine.py    faster-whisper con encoder de longitud dinámica
  recorder.py  grabación con pw-record (PipeWire)
  output.py    portapapeles (wl-copy) y teclado virtual uinput para pegar
  feedback.py  sonidos sintetizados (burbuja, moderno, clásico) y notificaciones
  install.py   servicio systemd, atajos de GNOME y regla udev
  config.py    configuración TOML
```
