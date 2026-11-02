// Chat: placeholder until phase B builds this view.

export default {
  id: 'chat',
  title: 'Chat',
  subtitle: 'Local Qwen chat. Nothing is uploaded.',
  mount(root, ctx) {
    root.classList.add('v-chat');
    root.append(ctx.ui.emptyState({icon: 'chat', title: 'Coming in the next step', text: 'This section is not built yet.'}));
  },
  unmount() {},
};
