# Chat memory

The chat agent can keep short facts across chats: who you are, what you prefer, and facts about your setup. It can
also search your earlier chats. Everything stays on this Mac, in the `outputs/memory` folder of the app. No model is
downloaded for it, and it works offline.

The full rules are in [AGENT.md](../AGENT.md#memory-and-chat-search). This page tells you how to use them.

## What the memory holds

The memory has two stores. Each store is a list of short entries.

| Store | Holds | Size |
|---|---|---|
| User profile | Who you are and what you prefer. | 800 characters |
| Notes | Facts about your setup and lessons from earlier chats. | 1,250 characters |

An entry is one line of at most 300 characters. Each entry also uses 3 characters for its line mark. The stores are
small on purpose: the model reads them at the start of each chat, and a 32K context has little room.

## How a chat uses the memory

1. When a chat starts, the model gets a copy of both stores in its instructions.
2. The copy does not change during that chat. A change made during the chat shows from the next chat.
3. When you tell the model a preference or a fact, or ask it to remember something, it can call the memory tool.
4. The memory tool asks you first. The approval card shows the change: a `+` line for a new entry, a `-` line for a
   removed one, and how full the store is before and after.
5. Press "Allow once" to save the change, or "Deny" to drop it.

Memory is on in Agent and Workstation chats. It is off in General chats, unless you turn on "Memory in General
chats". A temporary chat has no memory.

When a store is full, the memory tool does not save the new entry. The model must merge or remove entries first.

## What the memory never saves

The memory tool refuses an entry that:

- looks like a secret: an API key, a token, a password or a private key,
- reads like orders to an AI model, for example "ignore the previous instructions",
- has hidden characters.

Do not ask the model to remember passwords or keys. Use a password manager for them.

## When the answer says it saved but did not

A local model can write "I saved that to memory" without a call to the memory tool. Then nothing was saved. The app
checks each answer of an Agent or Workstation chat. When the answer says that it saved something and no memory call
ran, the answer shows a note: "This answer says that it saved something to memory, but no memory call ran. Nothing
was saved."

To save it:

1. Ask again, for example: `Save this in your memory: I prefer metric units.`
2. Or add the entry yourself in Settings > Chat memory.

## Search your earlier chats

The model can search your earlier chats with the `search_chats` tool. It finds words in the titles and in your and
the model's messages. It returns each chat's title, date and short parts of its messages.

- Case and accents do not matter. `istanbul` finds `İSTANBUL'da`, and `sikayet` finds `şikâyet`.
- A word also finds the longer words that start with it: `kedi` finds `kedimi`.
- It never finds the chat you are in, and it never indexes a temporary chat.
- It runs without asking. It does not search the model's thinking or the tool results.

Example request: `What did we decide about the lighthouse video in an earlier chat?`

## Change the memory in Settings

Open Settings (Cmd-6) and go to "Chat memory".

| Setting | Default | What it does |
|---|---|---|
| Memory | on | Off: no chat sees or changes the memory, and chat search is off. |
| Ask before saving | on | Each change shows an approval card first. Off: the model saves without asking, except in a chat that has web pages or files in it. |
| Memory in General chats | off | General chats see the memory too. |

The settings apply at once. A chat that is open keeps the memory it started with.

To add an entry:

1. Type the entry in the field under the store.
2. Press Add, or press Return.

To change an entry:

1. Press Edit next to the entry.
2. Change the text.
3. Press Save, or press Return. Press Escape to cancel.

To delete an entry, press the trash button next to it.

To delete every entry, press "Clear all" and confirm. You cannot undo it. Your earlier chats stay as they are.

The chat search index updates before each search. When search gives wrong results, press "Rebuild index".

## Files

| File | What it holds |
|---|---|
| `outputs/memory/memory.json` | The two stores. |
| `outputs/memory/settings.json` | The three settings. |
| `outputs/memory/chats.sqlite3` | The chat search index. You can delete it: the app builds it again. |
| `outputs/chats/<id>/memory.txt` | The copy of the memory that the chat started with. |
