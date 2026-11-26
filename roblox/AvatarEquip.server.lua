-- Avatar Finder: owner-only outfit equip command for your own Roblox experience.
-- Install this Script in ServerScriptService. Set OWNER_USER_ID before publishing.
-- In the experience chat, use: /wear <saved outfit ID>

local Players = game:GetService("Players")
local TextChatService = game:GetService("TextChatService")

-- Replace 0 with your Roblox user ID (the number shown on your Roblox profile).
local OWNER_USER_ID = 0

local COMMAND_NAME = "AvatarFinderWearCommand"
local equippedOutfitId: number? = nil
local busy = false

if OWNER_USER_ID <= 0 then
	warn("[Avatar Finder] Set OWNER_USER_ID in AvatarEquip.server.lua before using /wear.")
end

local function applyOutfit(player: Player, outfitId: number)
	if player.UserId ~= OWNER_USER_ID or busy then
		return
	end
	busy = true

	local ok, descriptionOrError = pcall(function()
		return Players:GetHumanoidDescriptionFromOutfitIdAsync(outfitId)
	end)
	if not ok then
		warn("[Avatar Finder] Could not load outfit " .. outfitId .. ": " .. tostring(descriptionOrError))
		busy = false
		return
	end

	local description = descriptionOrError
	local character = player.Character or player.CharacterAdded:Wait()
	local humanoid = character:WaitForChild("Humanoid", 10)
	if not humanoid or not humanoid:IsA("Humanoid") then
		warn("[Avatar Finder] No Humanoid found for the owner.")
		description:Destroy()
		busy = false
		return
	end

	local applied, applyError = pcall(function()
		humanoid:ApplyDescriptionAsync(description)
	end)
	description:Destroy()
	busy = false

	if not applied then
		warn("[Avatar Finder] Could not equip outfit: " .. tostring(applyError))
		return
	end

	equippedOutfitId = outfitId
	print("[Avatar Finder] Equipped outfit " .. outfitId .. " for " .. player.Name .. " in this experience.")
end

local function registerOwner(player: Player)
	if player.UserId ~= OWNER_USER_ID then
		return
	end

	player.CharacterAdded:Connect(function()
		local outfitId = equippedOutfitId
		if outfitId then
			task.delay(0.5, function()
				if player.Parent then
					applyOutfit(player, outfitId)
				end
			end)
		end
	end)
end

Players.PlayerAdded:Connect(registerOwner)
for _, player in Players:GetPlayers() do
	registerOwner(player)
end

local command = TextChatService:FindFirstChild(COMMAND_NAME)
if not command then
	command = Instance.new("TextChatCommand")
	command.Name = COMMAND_NAME
	command.PrimaryAlias = "/wear"
	command.AutocompleteVisible = true
	command.Parent = TextChatService
end

command.Triggered:Connect(function(textSource: TextSource, unfilteredText: string)
	if OWNER_USER_ID <= 0 or not textSource or textSource.UserId ~= OWNER_USER_ID then
		return
	end

	local player = Players:GetPlayerByUserId(textSource.UserId)
	if not player then
		return
	end

	local idText = string.match(unfilteredText, "^%s*/wear%s+(%d+)%s*$")
		or string.match(unfilteredText, "^%s*(%d+)%s*$")
	local outfitId = idText and tonumber(idText)
	if not outfitId or outfitId <= 0 or outfitId % 1 ~= 0 then
		warn("[Avatar Finder] Use /wear followed by a saved outfit ID.")
		return
	end

	applyOutfit(player, outfitId)
end)
