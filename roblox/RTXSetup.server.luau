--[[
	ARENA//RTX — Roblox Edition
	==========================================
	A drop-in "ray tracing" layer for your Roblox game, in pure Luau.

	PART 1 — RTX LIGHTING PROFILE (native, free, instant)
	    Future lighting technology + soft shadows, environment IBL
	    (specular + diffuse), golden-hour atmosphere, bloom, sun rays,
	    subtle depth of field and a filmic color grade.

	PART 2 — BOUNCE TRACER (real rays, fake GI)
	    Roblox has no per-pixel ray-traced renderer exposed to Luau.
	    So this traces ACTUAL rays through your ACTUAL game world with
	    workspace:Raycast(): every emissive part fires rays at random
	    directions, we measure what they hit, and we splat pooled,
	    colored PointLights at the hit surfaces — lit in the color of
	    (emitter × surface albedo). It's one-bounce global illumination,
	    the cheapest honest thing that is still really ray tracing.

	    Pooled, budgeted (RaysPerFrame), cleaned up, production-safe.

	Install:  see roblox/README.md
	Made by:  Arena.ai — an homage to real-time ray tracing
--]]

local Lighting          = game:GetService("Lighting")
local Workspace         = game:GetService("Workspace")
local RunService        = game:GetService("RunService")
local CollectionService = game:GetService("CollectionService")

-- ─────────────────────────── CONFIG ───────────────────────────
local CFG = {
	-- Part 1: lighting profile
	ApplyLightingProfile   = true,
	ClockTime              = 17.6,   -- 17.6 = golden hour ray-traced drama
	GeographicLatitude     = 52,     -- Braunschweig energy

	-- Part 2: bounce tracer (set false for lighting-only, zero raycasts)
	EnableBounceTracer     = true,
	RaysPerFrame           = 12,     -- rays fired per traced step (30 Hz)
	MaxBounceLights        = 48,     -- hard cap on pooled bounce lights
	BounceLifetime         = 2.0,    -- seconds a splat lives before fading
	BounceRange            = 9,      -- stud range of each bounce light
	BounceBrightness       = 1.15,   -- global gain for splats
	RayDistance            = 60,     -- how far rays travel
	AutoRegisterNeon       = true,   -- every Material.Neon part = light source
	SourceTag              = "RTXLight", -- CollectionService tag for manual sources
	MinSourceSize          = 0.5,    -- ignore tiny neon specks (studs)
	SourceBrightness       = 2.2,    -- default intensity for auto sources

	-- Console output
	Verbose                = true,
}

local function log(fmt, ...)
	if CFG.Verbose then
		print(string.format("[RTX] " .. fmt, ...))
	end
end

-- ───────────────────── PART 1 — RTX LOOK ─────────────────────
local function applyLightingProfile()
	assert(Lighting ~= nil, "[RTX] Lighting service unavailable")

	-- Future = Roblox's most physically-plausible native lighting
	local okTech, techErr = pcall(function()
		Lighting.Technology = Enum.Technology.Future
	end)
	if okTech then
		log("Lighting.Technology = Future")
	else
		warn("[RTX] Could not set Technology=Future (older client?) — keeping:", techErr)
	end

	Lighting.GlobalShadows        = true
	Lighting.ShadowSoftness       = 0.72
	Lighting.Brightness           = 2.6
	Lighting.ExposureCompensation = 0.12
	Lighting.EnvironmentDiffuseScale  = 0.85 -- IBL ambient from sky
	Lighting.EnvironmentSpecularScale = 0.9  -- sky reflections on glossy/metal
	Lighting.ClockTime            = CFG.ClockTime
	Lighting.GeographicLatitude   = CFG.GeographicLatitude
	Lighting.Ambient              = Color3.fromRGB(38, 44, 58)   -- cool shadow fill
	Lighting.OutdoorAmbient       = Color3.fromRGB(74, 82, 96)
	Lighting.ColorShift_Top       = Color3.fromRGB(255, 244, 224) -- warm key from sun
	Lighting.ColorShift_Bottom    = Color3.fromRGB(70, 84, 110)   -- blue dome bounce

	-- One atmosphere to rule the air
	local atmo = Lighting:FindFirstChildOfClass("Atmosphere")
	if not atmo then
		atmo = Instance.new("Atmosphere")
		atmo.Parent = Lighting
	end
	atmo.Density = 0.32
	atmo.Offset  = 0.18
	atmo.Haze    = 1.6
	atmo.Glare   = 0.4
	atmo.Color   = Color3.fromRGB(205, 220, 235)
	atmo.Decay   = Color3.fromRGB(255, 232, 210)

	-- Post FX stack (created once, reused on re-run)
	local function fx(className)
		local f = Lighting:FindFirstChild("RTX_" .. className)
		if not f then
			f = Instance.new(className)
			f.Name = "RTX_" .. className
			f.Parent = Lighting
		end
		return f
	end

	local bloom = fx("BloomEffect")
	bloom.Intensity = 0.62
	bloom.Size      = 26
	bloom.Threshold = 0.82

	local grade = fx("ColorCorrectionEffect")
	grade.Brightness =  0.02
	grade.Saturation =  0.12
	grade.Contrast   =  0.08
	grade.TintColor  =  Color3.fromRGB(255, 250, 245)

	local sun = fx("SunRaysEffect")
	sun.Intensity = 0.09
	sun.Spread    = 0.72

	local dof = fx("DepthOfFieldEffect")
	dof.FarIntensity  = 0.12
	dof.InFocusRadius = 42
	dof.FocusDistance = 30
	dof.NearIntensity = 0.05

	log("RTX lighting profile applied (golden hour, IBL, soft shadows, ACES-ish grade)")
end

-- ───────────────────── PART 2 — BOUNCE TRACER ─────────────────────
local sources = {}
local pool = {}
local poolFolder = nil
local sourceList = {}
local rayCursor = 1
local rng = Random.new()

local function buildPool()
	if poolFolder then return end
	poolFolder = Instance.new("Folder")
	poolFolder.Name = "RTXBouncePool"
	poolFolder.Parent = Workspace
	for i = 1, CFG.MaxBounceLights do
		local anchor = Instance.new("Part")
		anchor.Name        = string.format("RTXBounce_%02d", i)
		anchor.Anchored    = true
		anchor.CanCollide  = false
		anchor.CanTouch    = false
		anchor.CanQuery    = false -- invisible to raycasts, zero self-interference
		anchor.Transparency = 1
		anchor.Size        = Vector3.new(0.2, 0.2, 0.2)
		anchor.Material    = Enum.Material.SmoothPlastic
		anchor.CFrame      = CFrame.new(0, -500, 0) -- parked in the underworld
		local light = Instance.new("PointLight")
		light.Name    = "Bounce"
		light.Range   = CFG.BounceRange
		light.Brightness = 0
		light.Shadows = false -- soft fake GI, no second shadow map cost
		light.Parent  = anchor
		anchor.Parent = poolFolder
		table.insert(pool, { anchor = anchor, light = light, life = 0, baseBrightness = 0 })
	end
	log("Bounce pool ready: %d pooled lights", CFG.MaxBounceLights)
end

local function addSource(part)
	if not part:IsA("BasePart") then return end
	if sources[part] then return end
	if part.Size.Magnitude < CFG.MinSourceSize then return end
	local intensityAttr = part:GetAttribute("RTXIntensity")
	local colorAttr     = part:GetAttribute("RTXColor")
	local pulseAttr     = part:GetAttribute("RTXPulse")
	sources[part] = {
		intensity = (typeof(intensityAttr) == "number" and intensityAttr) or CFG.SourceBrightness,
		tint      = (typeof(colorAttr) == "Color3" and colorAttr) or part.Color,
		pulse     = (typeof(pulseAttr) == "boolean" and pulseAttr) or false,
		phase     = math.random() * math.pi * 2,
	}
	sourceList = nil -- force rebuild (cheap, lazily)
end

local function releaseSource(part)
	if sources[part] then
		sources[part] = nil
		sourceList = nil
	end
end

local function rebuildSourceList()
	sourceList = {}
	for part in pairs(sources) do
		if part.Parent then
			table.insert(sourceList, part)
		else
			sources[part] = nil -- streamed out / destroyed
		end
	end
end

local raycastParams = RaycastParams.new()
raycastParams.FilterType      = Enum.RaycastFilterType.Exclude
raycastParams.IgnoreWater     = true

local function rebuildExcludeList()
	local exclude = {}
	for part in pairs(sources) do
		table.insert(exclude, part)
	end
	raycastParams.FilterDescendantsInstances = exclude
end

local function acquireSlot()
	for _, slot in ipairs(pool) do
		if slot.life <= 0 then
			return slot
		end
	end
	return nil
end

local function sourceIntensityAt(src, info)
	if not info.pulse then return info.intensity end
	-- breathing neon, like the arena page hero
	return info.intensity * (0.65 + 0.35 * math.sin(os.clock() * 2.1 + info.phase))
end

local function castBounce(src, info)
	local direction = rng:NextUnitVector()
	local origin    = src.Position
	local result    = Workspace:Raycast(origin, direction * CFG.RayDistance, raycastParams)
	if not result then return end

	local hitPart = result.Instance
	local albedo  = hitPart:IsA("BasePart") and hitPart.Color or Color3.new(1, 1, 1)

	-- luminance-weighted bounce — dark velvet drinks light, white marble sings it back
	local albLum = 0.2126 * albedo.R + 0.7152 * albedo.G + 0.0722 * albedo.B
	local slot   = acquireSlot()
	if not slot then return end

	local splatColor = info.tint:Lerp(albedo, 0.5)
	local intensity  = sourceIntensityAt(src, info) * (0.25 + 0.75 * albLum) * CFG.BounceBrightness

	slot.anchor.CFrame    = CFrame.new(result.Position + result.Normal * 0.5)
	slot.light.Color      = splatColor
	slot.light.Range      = CFG.BounceRange
	slot.baseBrightness   = math.clamp(intensity, 0, 8)
	slot.light.Brightness = slot.baseBrightness
	slot.life             = CFG.BounceLifetime
end

local TRACE_HZ = 30 -- trace at most 30 times per second, however fast Heartbeat is
local excludeRefreshAcc = 0
local traceAcc = 0
local function tickBounce(dt)
	-- fade splats out quadratically, return them to the pool
	for _, slot in ipairs(pool) do
		if slot.life > 0 then
			slot.life = slot.life - dt
			local k = math.clamp(slot.life / CFG.BounceLifetime, 0, 1)
			slot.light.Brightness = slot.baseBrightness * k * k
			if slot.life <= 0 then
				slot.light.Brightness = 0
				slot.anchor.CFrame = CFrame.new(0, -500, 0)
			end
		end
	end

	traceAcc = traceAcc + dt
	if traceAcc < (1 / TRACE_HZ) then return end
	traceAcc = 0

	if sourceList == nil then rebuildSourceList() end
	local count = #sourceList
	if count == 0 then return end

	-- refresh occluder exclusion ~2x per second, not per ray
	excludeRefreshAcc = excludeRefreshAcc + (1 / TRACE_HZ)
	if excludeRefreshAcc >= 0.5 then
		excludeRefreshAcc = 0
		rebuildExcludeList()
	end

	for _ = 1, CFG.RaysPerFrame do
		local src = sourceList[rayCursor]
		rayCursor = rayCursor + 1
		if rayCursor > count then rayCursor = 1 end
		local info = sources[src]
		if info then
			castBounce(src, info)
		end
	end

end

local function bootBounceTracer()
	buildPool()

	-- manual sources: anything tagged via CollectionService
	for _, obj in ipairs(CollectionService:GetTagged(CFG.SourceTag)) do
		addSource(obj)
	end
	CollectionService:GetInstanceAddedSignal(CFG.SourceTag):Connect(addSource)
	CollectionService:GetInstanceRemovedSignal(CFG.SourceTag):Connect(releaseSource)

	-- auto sources: every Neon part in the world
	if CFG.AutoRegisterNeon then
		for _, obj in ipairs(Workspace:GetDescendants()) do
			if obj:IsA("BasePart") and obj.Material == Enum.Material.Neon then
				addSource(obj)
			end
		end
	end
	Workspace.DescendantAdded:Connect(function(obj)
		if obj:IsA("BasePart") then
			if CFG.AutoRegisterNeon and obj.Material == Enum.Material.Neon then
				addSource(obj)
			end
			if CollectionService:HasTag(obj, CFG.SourceTag) then
				addSource(obj)
			end
		end
	end)
	Workspace.DescendantRemoving:Connect(releaseSource)

	rebuildSourceList()
	rebuildExcludeList()
	log("Bounce tracer live — %d source(s) registered, budget %d rays/frame",
		sourceList and #sourceList or 0, CFG.RaysPerFrame)

	RunService.Heartbeat:Connect(tickBounce)
end

-- ─────────────────────────── BOOT ───────────────────────────
if CFG.ApplyLightingProfile then
	applyLightingProfile()
end
if CFG.EnableBounceTracer then
	bootBounceTracer()
end

log("ARENA//RTX online. Say hello to golden hour.")
