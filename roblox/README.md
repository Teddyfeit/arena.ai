# Avatar Finder → Roblox Studio

This folder contains an optional Roblox Studio server script for trying a public saved outfit **inside your own Roblox experience**.

## Install

1. Open your experience in Roblox Studio.
2. In Explorer, add a **Script** under `ServerScriptService`.
3. Copy `AvatarEquip.server.lua` into that Script.
4. Change `OWNER_USER_ID = 0` to your numeric Roblox user ID.
5. Use Roblox's **TextChatService** chat system, then run the experience.
6. In the website, open a saved outfit and copy its numeric Outfit ID.
7. In your experience's chat, enter `/wear <outfit ID>` (for example, `/wear 123456789`).

The server checks the Roblox user ID, so only the configured owner can run the command. The last selected outfit is reapplied after that player's character respawns.

## Important limits

- This equips the outfit only on your character **inside the experience where you installed the script**. Other players in that experience may see it.
- It does **not** change your account-wide Roblox avatar, your profile, or your avatar in other games. Roblox does not let an ordinary website do that without Roblox account authorization.
- The website and Roblox Studio/game do not communicate directly; copy the outfit ID from the website and enter the command in your experience.
- Use public outfits and respect Roblox's moderation and creator rules.
