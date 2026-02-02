export default {
  props: {
    host_data: Object,
    host: String,
  },
  template: `
    <li class="list-row">
      <div class="font-mono text-sm">{{ host }} 
        <div class="badge badge-sm" :class="{
          'badge-success': host_data.status === 'ok',
          'badge-error': host_data.status != 'ok'
        }">{{ host_data.status }}</div>
      </div>
    </li>
  `
}