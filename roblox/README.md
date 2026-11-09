# ARENA//RTX — Roblox Edition

Eine Drop-in-Raytracing-Schicht für dein Roblox-Spiel — in reinem Luau.

**Ehrlicher Reality-Check vorab:** Roblox expose *keine* per-Pixel-Raytracing-Renderer
an Luau (Stand 2026). Was es gibt, nutzt dieses Script maximal aus — und kombiniert es
mit echten Raycasts durchs Spiel. Das Ergebnis spielt optisch in einer anderen Liga,
ohne dass du dein Spiel neu bauen musst.

## Was drin ist

### Teil 1 — RTX Lighting Profile (nativ, gratis, sofort)
- `Lighting.Technology = Future` (Roblox' physikalisch plausibelstes natives Lighting:
  weiche Schatten, emissive Materialien, IBL)
- `ShadowSoftness` 0.72 · `EnvironmentSpecularScale` / `EnvironmentDiffuseScale`
  (Skybox wird zur Image-Based-Light-Quelle — Metall & Glas glänzen echt)
- Golden-Hour-Sonnenstand, warmer Key `ColorShift_Top`, blauer Dom-Bounce
- `Atmosphere` mit Glare & Haze, `BloomEffect`, `SunRaysEffect`,
  dezenter `DepthOfFieldEffect`, `ColorCorrectionEffect` (filmischer Grade)

### Teil 2 — Bounce Tracer (echte Strahlen, simuliertes GI)
Jede emissive Lichtquelle feuert per `workspace:Raycast()` echte Strahlen in echte
Geometrie. Trifft ein Strahl eine Fläche, entsteht am Treffpunkt ein gepooltes,
gefärbtes `PointLight` — in der Farbe `Emitlight × Oberflächen-Albedo`.
Einblick: ein-Bounce-Global-Illumination, bei der dunkler Samt Licht schluckt
und weißer Marmor es zurücksingt.

- **Gepoolt:** max. `MaxBounceLights` Lichter, keine Instanz-Flut
- **Gebudgetiert:** 30 Hz Tracing, `RaysPerFrame` Strahlen pro Schritt
- **Selbstreinigend:** Splats faden quadratisch aus und kehren in den Pool zurück
- **Server-seitig:** alle Clients sehen dieselbe Beleuchtung, keine Replikations-Hokuspokus

## Installation

### Option A — Studio, 30 Sekunden
1. Roblox Studio → dein Place öffnen
2. `ServerScriptService` → `+` → **Script** → Name: `RTXSetup`
3. Kompletten Inhalt von `RTXSetup.server.luau` hineinkopieren
4. Play drücken. Fertig. In der Output-Konsole steht `[RTX] ARENA//RTX online.`

### Option B — Rojo / Argon (Repo-Sync)
`roblox/RTXSetup.server.luau` mappt direkt auf ein `Script` in `ServerScriptService`
mit `RunContext = Server`. Einfach syncen.

### Option C — Nur der Look, zero Raycasts
In `CFG` setzen: `EnableBounceTracer = false` — dann bleiben nur die nativen Lichtsettings.

## Lichtquellen steuern

**Automatisch:** jedes `BasePart` mit `Material = Neon` ist eine Lichtquelle.

**Manuell:** beliebige Parts mit dem CollectionService-Tag `RTXLight`.

**Attribute** (auf jedem Quell-Part setzbar, Studio → Attribute):

| Attribut       | Typ      | Default            | Wirkung                        |
|----------------|----------|--------------------|--------------------------------|
| `RTXIntensity` | number   | `CFG.SourceBrightness` (2.2) | Helligkeit der Quelle        |
| `RTXColor`     | Color3   | `Part.Color`       | Bounce-Tönung überschreiben    |
| `RTXPulse`     | boolean  | `false`            | Atmen wie ein Neonherz         |

## Sofort-Demo

Füge einen zweiten Script in `Workspace` ein (oder paste in die Kommandozeile im Play-Modus):

```lua
local function orb(pos, color)
	local p = Instance.new("Part")
	p.Shape = Enum.PartType.Ball
	p.Size = Vector3.new(4, 4, 4)
	p.Material = Enum.Material.Neon
	p.Color = color
	p.Anchored = true
	p.CFrame = CFrame.new(pos)
	p:SetAttribute("RTXPulse", true)
	p.Parent = workspace
end
orb(Vector3.new(0, 8, 0), Color3.fromRGB(255, 40, 120))   -- magenta Herz
orb(Vector3.new(10, 4, 6), Color3.fromRGB(40, 220, 255))   -- cyan Satellit
orb(Vector3.new(-10, 4, 6), Color3.fromRGB(255, 190, 80))  -- ember dritter Akt
```

Die Umgebung über den Orbits fängt sofort an, in denselben Farben zu glühen.

## Performance

| Knopf | Default | Effekt |
|---|---|---|
| `RaysPerFrame` | 12 | Strahlen pro Tick (30 Hz) → 360/s |
| `MaxBounceLights` | 48 | Pool-Größe; für Mobile auf 24 senken |
| `BounceLifetime` | 2.0 s | je kürzer, desto flackeriger/lebendiger |
| `BounceRange` | 9 Studs | Radius der Bounce-Lichter |
| `AutoRegisterNeon` | true | false = nur getaggte Quellen |

Faustregel: Raycasts sind in C++ und kosten anständig wenig, aber 360/s × große Welten
addieren sich. Auf fremden Maps: `RaysPerFrame = 6`, `MaxBounceLights = 24`.

## Was es NICHT ist (ehrlich bleiben)

- Kein per-Pixel Path Tracer in den Backbuffer (dafür gibt es keine öffentliche API —
  Community-Projekte wie *Penumbra* experimientieren damit, in Echtzeit mit starken
  Abstrichen)
- Keine echten Spiegelungen/Refraktionen — der Glanz kommt aus
  `EnvironmentSpecularScale` + `Material = Metal/Glass` + `Reflectance`
- Bounce-Lichter sind GI-Approximation, keine Energieverfolgung pro Sample

Im Gegenzug: Es läuft auf echten Geräten von echten Spielern, heute, im Live-Server.

## Dateien

- `RTXSetup.server.luau` — alles in einem Script, serverseitig
