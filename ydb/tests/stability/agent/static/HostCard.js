export default {
  props: {
    host_data: Object,
    host: String,
  },
  template: `
    <div class="flex justify-between items-center p-2 bg-base-100 shadow rounded-box mb-2">
      <span class="font-mono text-sm">{{ host }}</span>
      <div class="badge badge-sm" :class="{
        'badge-success': host_data.status === 'ok',
        'badge-error': host_data.status != 'ok'
      }">{{ host_data.status }}</div>
    </div>
  `
}