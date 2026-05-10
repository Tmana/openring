# OpenRing — Hardware

Reference build for a v0.1 doorbell. Substitute freely; OpenRing only
requires a Linux host with a camera that speaks RTSP and a GPIO button.

## Bill of materials

Prices below are 2026-05 snapshots from US vendors and will drift.
Prefer the linked source-of-truth when buying — the unit-price column
is for napkin math, not contracts. Items 4 and 6 (button + enclosure)
can be salvaged from a sacrificial Ring; see § Donor reuse below.

| # | Part | Vendor (link) | Notes | USD |
|---|---|---|---|---|
| 1 | Raspberry Pi Zero 2 W | [PiShop.us](https://www.pishop.us/product/raspberry-pi-zero-2-w/) ($17.25), or any [Approved Reseller](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) for the $15 MSRP | Get the W — Wi-Fi is mandatory. Stock is intermittent; [rpilocator.com](https://rpilocator.com/) tracks live availability. | 17 |
| 2 | Pi Camera Module 3 Wide NoIR | [PiShop.us](https://www.pishop.us/product/raspberry-pi-camera-module-3-wide-noir/) / [Adafruit 5660](https://www.adafruit.com/product/5660) | 102° HFOV; NoIR variant has no IR-cut filter so cheap 850 nm IR LEDs give you night vision. Ships with both 200 mm and 150 mm flex cables. | 38 |
| 3 | 32 GB SanDisk Industrial microSD | [Amazon B07BYWYG2J](https://www.amazon.com/dp/B07BYWYG2J) (SDSDQAF3-032G-I, ~$30) | Industrial-grade MLC NAND survives the write churn of journald + MediaMTX's small caches. Consumer SDs (~$8) work for a few months and then die mid-event-stream. | 30 |
| 4 | 12 mm momentary stainless panel-mount button (NO, IP65, screw-terminal) | [Amazon B07HG4S2MX](https://www.amazon.com/dp/B07HG4S2MX) (single, ~$7) or [APIELE 12 mm momentary](https://www.apiele.com/collections/12mm-momentary) for 5-pack value | The actual doorbell. **Free if salvaging a Ring** — see § Donor reuse below. | 7 |
| 5 | Mean Well IRM-15-5 (or equivalent 5 V/3 A buck) | [DigiKey 7704662](https://www.digikey.com/en/products/detail/mean-well-usa-inc/IRM-15-5/7704662) ($8.60, 2k+ in stock) | Steps existing chime transformer's 16-24 V AC down to 5 V DC. Universal 85-305 VAC input so it also works behind a USB wall wart. | 9 |
| 6 | 3D-printed enclosure | DIY (PETG filament; STLs in `hardware/` TBD) | PETG holds up to porch sun; PLA does not. **Free if salvaging a Ring shell** — see § Donor reuse. | filament |
| 7 | M2.5 hardware kit + JST PH connectors + 22 AWG silicone hookup wire | Any [assorted grab bag on Amazon](https://www.amazon.com/s?k=m2.5+screw+kit) | One-time investment; you'll have leftovers for the next ten projects. | 10 |
| | **Subtotal v0.1** | | (donor path: ~$94) | **~$111** |

### Add-ons (v0.3 — two-way audio)

| # | Part | Vendor (link) | Notes | USD |
|---|---|---|---|---|
| 8 | UGREEN USB audio adapter | [Amazon B01N905VOY](https://www.amazon.com/dp/B01N905VOY) | Pi Zero 2 W has no analog audio out; this is the smallest USB DAC that works with `aplay` out of the box. USB-A; pair with a micro-USB OTG cable, or buy the USB-C variant if your build uses a USB-C breakout. | 9 |
| 9 | INMP441 I2S mic *or* USB lavalier mic | [Amazon B0GFFFMDBW](https://www.amazon.com/dp/B0GFFFMDBW) (3-pack INMP441, ~$10 = $3.50/ea) | Either works; USB is plug-and-play, I2S is one fewer USB port to find. The 3-pack pays for itself the first time you fry one. | 4 |
| 10 | Mini Oval 1 W 8 Ω speaker | [Adafruit 4227](https://www.adafruit.com/product/4227) ($1.95) | Anything tiny works; voice-only audio is forgiving. Plugs into the UGREEN's 3.5 mm jack (impedance mismatch is real but ok at conversational volumes). | 2 |
| | **+v0.3 subtotal** | | | **+~$15** |

## Power options, ranked

1. **Existing wired doorbell transformer (recommended).** Most North
   American houses have a 16 V or 24 V AC transformer in the basement
   or attic feeding the chime. Swap the chime for a buck converter and
   you're done — the doorbell still rings (we'll wire a small relay on
   the GPIO output if you want the analog chime to keep working in
   parallel; that's a v0.2 wiring guide).
2. **5 V USB-C wall wart run through the wall.** Easiest if you don't
   already have doorbell wiring.
3. **PoE.** Uses a $15 PoE splitter; nice if you've already run Cat6 to
   the porch for an IP camera. Adds bulk to the enclosure.

## Donor reuse — salvaging an old Ring

You cannot run OpenRing's firmware on a Ring's locked SoC. The source
code Amazon publishes at [ring.com/oss](https://ring.com/oss) is the
GPL-compliance disclosure (modified Linux kernel, BusyBox, etc.) — not
the Ring application stack. The application is closed-source and the
bootloader is signed on every model since ~2017. So forget flashing.

But the doorbell *shell, button, and power harness* are perfectly
reusable, and salvaging them shaves ~$15 off the v0.1 BOM and saves
you the 3D-printer time on the enclosure.

### What's reusable

| Donor part | What you save | Effort |
|---|---|---|
| Enclosure / mounting plate | BOM #6 (3D-printed shell). Already weather-rated, already drilled for your porch. | Low — Dremel out the original SoC tray to make room for a Pi Zero 2 W carrier. |
| Button + faceplate | BOM #4 ($7). | Medium — desolder from the original PCB; resolder to pigtail leads going to Pi GPIO. |
| Power harness + screw terminals | If your old Ring was wired to a 16-24 V AC chime transformer, the wiring is already in your wall. | Low — bypass the Ring's internal buck and feed AC into the Mean Well IRM-15-5 (BOM #5) instead. |
| IR LED ring (some models — Pro / Pro 2) | Cheap 850 nm night-vision illumination for the NoIR camera. | Medium — desolder ring; current-limit-drive from a Pi GPIO + small NPN transistor. |

### What's *not* reusable

- **Camera module.** The Ring's camera is bound to a proprietary ISP
  block on the SoC. No V4L2 / libcamera path exists even with root.
  Don't bother — Pi Camera Module 3 Wide NoIR (BOM #2) is the
  supported path and it costs $38.
- **The SoC, the wake MCU, and the main PCB.** Locked, signed,
  undocumented, and the wrong shape for OpenRing's architecture
  (which expects full Linux on the device side).
- **The battery, on battery models.** Ring's deep-sleep PIR-wake
  architecture is fundamentally different from OpenRing's
  always-on RTSP stream; the battery wouldn't last a day under
  continuous H.264 encoding.

### Tools you'll need

| Tool | Vendor (link) | USD | Skip if you have |
|---|---|---|---|
| T6 + T15 security Torx screwdriver set | [Amazon B0DZV8Y277](https://www.amazon.com/dp/B0DZV8Y277) | ~$8 | Ring-specific bits; a generic security-Torx kit also works |
| Plastic spudger / pry tool | iFixit kit, or any phone-repair set | ~$5 | An old guitar pick |
| Soldering iron + solder | [Pinecil v2](https://pine64.com/product/pinecil-smart-mini-portable-soldering-iron/) (~$30) | ~$30 | Any iron you already own |
| Multimeter (DMM) | Any cheap one — Astro AI on Amazon, ~$15 | ~$15 | Any DMM you already own |
| 22 AWG silicone hookup wire + heat-shrink assortment | Amazon grab bag | ~$10 | Already in BOM #7 |

**Donor-path additional cost: ~$8** (just the security-Torx set if you
already have an iron and a multimeter).

### Teardown sequence (generic)

These are model-agnostic. YouTube has model-specific videos for every
Ring generation; search "Ring Doorbell *N* teardown" and watch one for
**your** specific model before starting.

> **Safety:** disconnect AC power at your breaker before working on a
> wired unit. 16-24 V AC won't kill you but is plenty to ruin a Pi
> if miswired.

1. **Faceplate first.** Single T6 security screw at the bottom edge
   on most models. Lifts off to reveal the front PCB.
2. **PCB.** 4× T6 (sometimes T15) screws hold the PCB to the back
   shell. Note the orientation of every flex cable (camera, button,
   battery if present) before disconnecting — phone-photo each
   connector.
3. **Button assembly.** The doorbell button is typically a SPST
   momentary tactile switch with a long actuator stem, soldered
   directly to the front PCB. Cut the leads at the base of the
   switch (or fully desolder if you want it clean) and add 22 AWG
   pigtails. This is now your BOM #4 button.
4. **Power harness** (wired models only). The two screw terminals on
   the back accept your existing chime transformer leads. Cut the
   leads coming off the lugs and wire them to the AC IN pins on the
   IRM-15-5. **Do not** reuse the Ring's internal buck — its output
   topology and protection circuitry aren't documented and you don't
   want to find out the hard way that it back-feeds 12 V into your
   Pi.
5. **IR LEDs** (optional). If the donor has an IR ring at the camera
   bezel, desolder it. Meter for forward voltage with the multimeter
   so you know what current-limiting resistor to pair with the GPIO
   driver transistor.

### Wiring the salvaged button to the Pi

OpenRing's firmware reads the button as `gpiozero.Button(17, pull_up=True)`
— wire one terminal of the salvaged switch to **GPIO 17** (header pin
11) and the other to **GND** (any ground pin, e.g. pin 9). The
debounce, press-handler, and HMAC-signed publish-to-host code is
already in `services/doorbell-firmware/src/button.py`.

### Wiring the salvaged power harness

Cut the chime-transformer leads off the Ring's back-shell terminals.
Wire them to **AC IN (L) and AC IN (N)** on the IRM-15-5; AC is
non-polarized so order doesn't matter. The IRM-15-5's **+V** and
**-V (COM)** outputs go to the Pi's **5V (header pin 2)** and **GND
(pin 6)** — or, more robustly, to a hard-wired micro-USB cable
plugged into the Pi's PWR port (this gives you the Pi's onboard
inrush limiter and TVS diode).

> Always verify polarity and 4.95-5.10 V on the IRM-15-5 output with
> a multimeter **before** connecting the Pi for the first time. Cap
> all unused conductors with heat-shrink.

### Donor compatibility — tested-on

Add a row when you donate one!

| Ring model | Year | Reusable | Notes |
|---|---|---|---|
| Ring Doorbell (gen 1) | 2014–2017 | enclosure, button, transformer | T6 only; battery is glued in but not needed |
| Ring Doorbell 2 | 2017 | enclosure, button, transformer | Same teardown as gen 1 |
| Ring Doorbell Pro | 2017+ | enclosure, button, transformer, IR LEDs | T6 + T15 needed; PCB is more densely packed |
| Ring Doorbell 3 / 4 | 2020+ | enclosure, button | Anti-tamper screws on internal flex; harder teardown |
| Ring Doorbell Pro 2 | 2021+ | enclosure, button | Verified bootloader; absolutely no firmware-level reuse |

## Why not battery?

Real-time H.264 + Wi-Fi on a Pi Zero 2 W draws ~1.5-2 W steady. A
modest 3000 mAh USB power bank gives you ~6 hours. The "1-year battery
life" of commercial doorbells comes from a totally different
architecture: deep-sleep with a PIR-driven wake, low-resolution wake
shots only. To match that you'd be writing custom firmware on an
ESP32-S3 with a separate PIR sensor and accepting that the live-view
feature ships a 5-second delay. Not the v0.1 product.

If demand is loud enough we'll fork an `openring-mini` track for an
ESP32-S3 build. Until then: get power to the porch.

## Why not a cellular fallback?

Same reason commercial-doorbell vendors don't ship one in the base
product: it's a recurring SIM cost and it makes the device dependent
on a third party (the carrier and our cell-modem firmware). OpenRing
declines that bargain. If your home internet is unreliable, the
correct fix is your home internet.

## Camera lens choices

| Lens | Field of view | Use case |
|---|---|---|
| Pi Camera Module 3 standard | 75° | Hallway / interior side door |
| Pi Camera Module 3 **wide** | 102° | Porch front door — recommended |
| Camera Module 3 with telephoto add-on | narrow | Long driveway gate |
| Arducam IMX477 + M12 lens | whatever | If you want pro glass and don't mind tinkering |

## Network requirements

- 2.4 GHz Wi-Fi (the Pi Zero 2 W doesn't speak 5 GHz). 802.11n is
  sufficient — H.264 720p15 fits comfortably under 4 Mbit/s.
- Static DHCP reservation for the doorbell, *or* mDNS (Avahi). We
  document both in v0.1.
- Port 8554 (RTSP) reachable from the host. Stays on the LAN — we
  don't expose the doorbell port externally.

## Tested-on (please add a row when you build one!)

| OpenRing version | Pi model | Camera | Power source | Notes |
|---|---|---|---|---|
| v0.1.0-dev | Pi Zero 2 W | Camera Module 3 Wide NoIR | 24 VAC chime → IRM-15-5 | reference build |
