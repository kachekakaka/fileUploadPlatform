export function detailPath(kind: 'works' | 'tasks', id: string, embedded = false) {
  return (embedded ? '/embed/' : '/') + kind + '/' + id
}
