export const sources = import.meta.glob(['../../app/*.py', '../browser_python/*.py'], {
  eager: true, import: 'default', query: '?raw',
});
