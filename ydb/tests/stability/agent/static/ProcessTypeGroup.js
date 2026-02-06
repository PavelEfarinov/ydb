import HostProcessItem from './HostProcessItem.js'

export default {
  components: {
    HostProcessItem
  },
  props: {
    type: String,
    description: String,
    hosts: Object, // { hostName: hostData }
    processes: Object // { hostName: [ProcessInfo] }
  },
  setup(props) {
    const { ref } = Vue
    const isExpanded = ref(false)
    const isEnabled = ref(false)
    const isDescriptionExpanded = ref(false)

    // Fetch initial schedule state
    axios.get('/api/schedule')
      .then(response => {
        if (response.data[props.type] !== undefined) {
          isEnabled.value = response.data[props.type]
        }
      })

    function toggleSchedule() {
      const newState = !isEnabled.value
      axios.post('/api/schedule', { type: props.type, enabled: newState })
        .then(() => {
          isEnabled.value = newState
        })
        .catch(err => {
          console.error('Failed to update schedule', err)
        })
    }

    function runProcess(host) {
      axios.post('/api/hosts/process', { host: host, type: props.type })
        .then(() => {
          console.log(`Started process ${props.type} on ${host}`)
        })
        .catch(err => {
          console.error(`Failed to start process ${props.type} on ${host}`, err)
        })
    }

    function getProcessesByTypeAndHost(host) {
      const hostProcs = props.processes[host] || []
      return hostProcs
        .sort((a, b) => b.id - a.id)
    }

    return {
      isExpanded,
      isEnabled,
      isDescriptionExpanded,
      toggleSchedule,
      runProcess,
      getProcessesByTypeAndHost
    }
  },
  template: `
    <div class="card bg-base-100 shadow-xl mb-6">
      <div class="card-body p-4">
        <div class="flex justify-between items-center cursor-pointer">
          <div class="flex-1">
            <div class="flex items-center gap-2">
              <h2 class="card-title text-xl">{{ type }}</h2>
              <input
                type="checkbox"
                :checked="isEnabled"
                @click.prevent="toggleSchedule"
                class="toggle"
                :class="isEnabled ? 'toggle-success' : 'toggle-neutral'"
              />
              <button
                v-if="description"
                @click="isDescriptionExpanded = !isDescriptionExpanded"
                class="btn btn-ghost btn-xs"
              >
                {{ isDescriptionExpanded ? '📖 Hide Info' : '📖 Info' }}
              </button>
            </div>
            <div v-if="description && isDescriptionExpanded" class="collapse collapse-open mt-2">
              <div class="collapse-content bg-base-200 rounded-box p-3">
                <p class="text-sm text-base-content/80 whitespace-pre-line">{{ description }}</p>
              </div>
            </div>
            <div class="flex items-center mt-2">
              <div class="tooltip tooltip-right" :data-tip="host" v-for="(hostData, host) in hosts">
              <div
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
            </div>
          </div>
          <button class="btn btn-ghost btn-sm" @click="isExpanded = !isExpanded">
            {{ isExpanded ? 'Collapse' : 'Expand' }}
          </button>
        </div>
        
        <div v-show="isExpanded" class="mt-4">
          <host-process-item
            v-for="(hostData, host) in hosts"
            :key="host"
            :host="host"
            :processes="getProcessesByTypeAndHost(host)"
            :is-scheduled="isEnabled"
            @run-process="runProcess"
          ></host-process-item>
        </div>
      </div>
    </div>
  `
}