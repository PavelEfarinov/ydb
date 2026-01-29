export default {
  props: {
    host_data: Object,
    host: String,
  },
  template: `
    <li class="flex justify-between items-center p-2 hover:bg-base-200 rounded-box">
      <div>
        <div class="font-mono text-sm">{{ host }}</div>
        <div class="badge badge-sm" :class="{
          'badge-success': host_data.status === 'ok',
          'badge-error': host_data.status != 'ok'
        }">{{ host_data.status }}</div>
      </div>
    </li>
  `
}