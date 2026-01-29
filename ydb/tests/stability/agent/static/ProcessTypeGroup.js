import HostProcessItem from './HostProcessItem.js'

export default {
  components: {
    HostProcessItem
  },
  props: {
    type: String,
    hosts: Object, // { hostName: hostData }
    processes: Object // { hostName: [ProcessInfo] }
  },
  setup(props) {
    const { ref } = Vue
    const isExpanded = ref(false)

    function getProcessesByTypeAndHost(host) {
      const hostProcs = props.processes[host] || []
      return hostProcs
        .sort((a, b) => b.id - a.id)
    }

    return {
      isExpanded,
      getProcessesByTypeAndHost
    }
  },
  template: `
    <div class="card bg-base-100 shadow-xl mb-6">
      <div class="card-body p-4">
        <div class="flex justify-between items-center cursor-pointer" @click="isExpanded = !isExpanded">
          <div>
            <h2 class="card-title text-xl">{{ type }}</h2>
            <div
              v-for="(hostData, host) in hosts"
              aria-label="status"
              class='status p-1 m-1'
              :class="{
              'status-success animate-bounce':(processes[host] ?? []).length > 0 && processes[host].at(-1).status === 'running',
              'status-success':(processes[host] ?? []).length > 0 && processes[host].at(-1).status === 'finished',
              '':(processes[host] ?? []).length == 0,
              'status-error':(processes[host] ?? []).length > 0 && ['failed', 'error'].includes(processes[host].at(-1).status),
              }">
            </div>
          </div>
          <button class="btn btn-ghost btn-sm">
            {{ isExpanded ? 'Collapse' : 'Expand' }}
          </button>
        </div>
        
        <div v-show="isExpanded" class="mt-4">
          <host-process-item 
            v-for="(hostData, host) in hosts" 
            :key="host"
            :host="host"
            :processes="getProcessesByTypeAndHost(host)"
          ></host-process-item>
        </div>
      </div>
    </div>
  `
}