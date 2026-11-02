// Settings: placeholder until phase B builds this view.

export default {
  id: 'settings',
  title: 'Settings',
  subtitle: 'Paths, prompt help and appearance',
  mount(root, ctx) {
    root.classList.add('v-settings');
    root.append(ctx.ui.emptyState({icon: 'settings', title: 'Coming in the next step', text: 'This section is not built yet.'}));
  },
  unmount() {},
};
