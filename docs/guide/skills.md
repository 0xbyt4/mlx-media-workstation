# Skills

A skill is a guide that teaches the agent one kind of task, step by step. A new chat lists the skills that are on.
When your request matches a skill, the agent loads its guide and follows it. You can also pick a skill yourself.

Skills use the open [Agent Skills format](https://agentskills.io/specification): a folder with a `SKILL.md` file.
Skills made for other agents work here too, when they use tools that this app has.

The technical rules are in [AGENT.md](../AGENT.md#skills).

## The skills of the app

| Skill | What it does | It needs |
|---|---|---|
| `short-film` | Makes a short film of 3 to 6 shots: images, the same character in new shots, video clips, voice, music, the mix and the joined film. | The full profile and the media tools. |
| `podcast-cleanup` | Cleans up a recording: shortens long silences, sets the loudness to -16 LUFS and writes show notes. | Shell on, a granted folder with the recording, and ffmpeg from Homebrew. |
| `subtitle-video` | Transcribes a video, writes an SRT subtitle file and adds a subtitle track to the video. | Transcribe (Qwen3-ASR). Shell for the track. |

All three work offline.

## Use a skill

The agent picks a skill by itself when your request matches it. For example: `Clean up interview.m4a in my folder`.

To pick a skill yourself:

1. Open an Agent chat.
2. Type `/skill`, a space and the skill's name at the start of the message.
3. Type your request after it, for example: `/skill subtitle-video Add English subtitles to trip.mp4`.
4. Press Return.

The guide goes in with your message. An unknown name is refused, and the error lists the skills that are on.

A chat does not list a skill when it lacks a tool that the skill needs. For example, `podcast-cleanup` needs Shell.
Turn the tool on in the chat's Tools menu, then start a new chat. With `/skill` the agent still gets the guide, and it
tells you what is missing.

The list of skills is fixed when a chat starts. A change in Settings > Skills applies to new chats.

## Turn skills on or off

1. Open Settings (Cmd-6).
2. Find the Skills section.
3. Use the switch of a skill.

The section shows each skill's description, the tools it needs, its scripts and its licence. Press "View SKILL.md" to
read the whole guide. A broken skill shows its error in red and cannot be turned on.

The note under the list says how much of the prompt the list of skills takes. The limit is 600 tokens. When more
skills are on, the ones that do not fit are left out of the list. They still work with `/skill`.

## Import a skill

Import only skills from people you trust. A skill is text that the agent follows.

1. Download or copy the skill to this Mac: a folder with a `SKILL.md`, or a `.zip`.
2. In Settings > Skills, enter the full path of the folder or the `.zip` under "Import a skill".
3. Press Check.
4. Read what the check shows: the files, the scripts, and the lines that look dangerous (for example a download that
   runs in a shell, "paste this in Terminal", or a long base64 text).
5. Press Import. When the check found something, confirm the dialog.

The imported skill is on for new chats. It is in `outputs/skills/<name>/`. To remove it, press Delete in its row.

The app never installs a skill from the internet, and it never runs a skill's scripts by itself. When the agent wants
to run a script, it reads the script and asks you through Python or Shell. The approval card shows the code.

## Use your own skills folder

You can keep skills in your own folder, for example `~/.agents/skills`, which other agents read too.

1. In Settings > Skills, enter the full path under "Your skills folder".
2. Press Save.
3. Read each new skill ("View SKILL.md"), then turn on the ones you want.

The skills of your folder start off. The app only reads the folder: it never changes or deletes those skills. An
imported skill with the same name replaces a skill of your folder or of the app.

## Write a skill

1. Make a folder whose name is the skill's name, for example `meeting-notes`. Use `a-z`, `0-9` and hyphens.
2. In it, make `SKILL.md`:

   ```markdown
   ---
   name: meeting-notes
   description: "Summarize meeting notes: decisions, owners and dates. Use when the user asks to summarize notes."
   allowed-tools: read_file
   ---

   # Meeting notes

   1. Read the notes with read_file.
   2. List the decisions, then the tasks with their owners and dates.
   ```

3. Import the folder, or put it in your skills folder.

- Put the description in quotes when it holds a colon.
- `allowed-tools` lists the tools the skill needs. A chat without them does not list the skill. It never approves a
  call.
- Keep the guide short: the agent reads all of it. Put long reference text in other files of the folder and name
  them in the guide.

## When something goes wrong

| Problem | What to do |
|---|---|
| The skill shows "Broken" | Read the error in its row. Fix `SKILL.md`, then open Settings again. |
| "name is ..., but the folder is ..." | The `name` in `SKILL.md` must be the same as the folder's name. |
| "not valid YAML" | Put quotes around a text that holds a colon. |
| The agent does not use a skill | Check that the skill is on and that the chat has the tools it needs. Start a new chat, or use `/skill <name>`. |
| "No skill named ... is on" | Check the name in Settings > Skills, and turn the skill on. |
