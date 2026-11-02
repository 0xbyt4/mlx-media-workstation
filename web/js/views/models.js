// Models: placeholder until phase B builds this view.

export default {
  id: 'models',
  title: 'Models',
  subtitle: 'What is installed, and what fits this Mac',
  mount(root, ctx) {
    root.classList.add('v-models');
    root.append(ctx.ui.emptyState({icon: 'models', title: 'Coming in the next step', text: 'This section is not built yet.'}));
  },
  unmount() {},
};
