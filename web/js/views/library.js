// Library: placeholder until phase B builds this view.

export default {
  id: 'library',
  title: 'Library',
  subtitle: 'Every output in outputs/',
  mount(root, ctx) {
    root.classList.add('v-library');
    root.append(ctx.ui.emptyState({icon: 'library', title: 'Coming in the next step', text: 'This section is not built yet.'}));
  },
  unmount() {},
};
