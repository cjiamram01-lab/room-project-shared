<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import mqtt, { type MqttClient } from 'mqtt'
import QRCode from 'qrcode'
import config from "../assets/config.json"

let debugEnabled = import.meta.env.DEV

function debugLog(...args: unknown[]) {
  if (debugEnabled) console.log(...args)
}

interface Teacher {
  officerid: string
  officerlogin: string
  prefixname: string
  officername: string
  officersurname: string
}

interface ScheduleItem {
  rowId:number,
  objective:string,
  roomcode: string
  weekday: string
  timeperiodfrom: string
  timeperiodto: string
  coursecode: string
  coursename: string
  revisioncode?: string
  periodfrom: string
  periodto: string
  teacher: Teacher[]
  id: string
  uuid: string
  startTime?: string
  finishTime?: string
  source?: string
  status: number
  usage_status?: number
}

type ConnStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const canvasRef      = ref<HTMLCanvasElement | null>(null)
const status         = ref<ConnStatus>('connecting')
const lastUpdated    = ref<string>('')
const lastTopic      = ref<string>('')
const lastStreamTopic = ref<string>('')
const errorMessage   = ref<string>('')
const qrSchedule     = ref<ScheduleItem | null>(null)
const scheduleItems  = ref<ScheduleItem[]>([])
const currentTime    = ref<string>('')
const rendering      = ref(false)
const activeItemId   = ref<string>('')
const nowMinutes     = ref(0)
const qrUrl          = ref<string>('')
const copied         = ref(false)
const lastUpdate     = ref<{ status: string; update: string } | null>(null)
const staffAccessLink = ref<string>('')
const staffUsingRoom  = ref(false)
const staffAccessSeconds = ref(0)
const staffSubjectCode = ref<string>('')
const staffUserName  = ref<string>('')
const closeQrCanvasRef = ref<HTMLCanvasElement | null>(null)
const closeUrl       = ref<string>('')
const closeCopied    = ref(false)
const showInstallButton = ref(false)
const personCount    = ref<number | null>(null)
const usageClosedMsg = ref<string>('')

let deferredInstallPrompt: Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }> } | null = null

let bookingUrl = ''
let prefixUrl  = ''
let staffAccessUrl = ''

let client: MqttClient | null = null
let mqttStreamSource: EventSource | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let clockTimer: ReturnType<typeof setInterval> | null = null
let staffAccessTimer: ReturnType<typeof setInterval> | null = null

function pad2(n: number) {
  return n.toString().padStart(2, '0')
}

function formatElapsed(totalSeconds: number) {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = totalSeconds % 60
  return h > 0 ? `${pad2(h)}:${pad2(m)}:${pad2(s)}` : `${pad2(m)}:${pad2(s)}`
}

function tickClock() {
  const now = new Date()
  currentTime.value = now.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
  nowMinutes.value = now.getHours() * 60 + now.getMinutes()
  if (now.getSeconds() === 0) syncActiveItem()
}

function getItemStatus(item: ScheduleItem): 'active' | 'upcoming' | 'finished' | 'confirmed' | 'closed' {
  if (!item.startTime || !item.finishTime) return 'upcoming'
  const start = timeToMinutes(item.startTime)
  const end   = timeToMinutes(item.finishTime)
  const now   = nowMinutes.value
  if (now < start) return 'upcoming'
  if (now > end)   return 'finished'
  if (item.usage_status === 3) return 'confirmed'
  if (item.usage_status === 4 || item.usage_status === 5) return 'closed'
  return 'active'
}
let apiBase = ''
let roomNo  = ''

// ---------------------------------------------------------------------------
// QR rendering (left pane)
// ---------------------------------------------------------------------------
async function renderQr(url: string) {
  if (!canvasRef.value) return
  rendering.value = true
  try {
    const size = canvasRef.value.offsetWidth || canvasRef.value.parentElement?.clientWidth || 320
    await QRCode.toCanvas(canvasRef.value, url, {
      width: size,
      margin: 2,
      color: { dark: '#000000', light: '#ffffff' },
    })
    lastUpdated.value = new Date().toLocaleTimeString()
    errorMessage.value = ''
  } catch (err) {
    errorMessage.value = `QR render error: ${(err as Error).message}`
  } finally {
    rendering.value = false
  }
}

async function renderCloseQr() {
  const params = new URLSearchParams({ room: roomNo })
  if (qrSchedule.value?.rowId != null) params.set('rowId', String(qrSchedule.value.rowId))
  if (qrSchedule.value?.objective) params.set('objective', qrSchedule.value.objective)
  closeUrl.value = `${window.location.origin}${import.meta.env.BASE_URL}submit-close?${params}`
  await nextTick()
  if (!closeQrCanvasRef.value) return
  try {
    await QRCode.toCanvas(closeQrCanvasRef.value, closeUrl.value, {
      width: 480,
      margin: 2,
      color: { dark: '#000000', light: '#ffffff' },
    })
  } catch {
    // non-critical — overlay still shows the timer
  }
}

async function copyCloseUrl() {
  if (!closeUrl.value) return
  await navigator.clipboard.writeText(closeUrl.value)
  closeCopied.value = true
  setTimeout(() => { closeCopied.value = false }, 1500)
}

function buildQrUrl(item: ScheduleItem): string {
  const rowIdParam = isItemActive(item) ? `rowId=${item.rowId}&` : ''
  const base = `${prefixUrl}?${rowIdParam}source=${item.source}&schedule_id=${item.id}&uuid=${item.uuid}&flag=${item.status}`
  return base
  // return item.status === 2 ? `${base}&flag-allow=true` : base
}

async function checkUsageStatus(scheduleId: string, sourceType: string): Promise<number | null> {
  try {
    const params = new URLSearchParams({ schedule_id: scheduleId, source_type: sourceType })
    const res = await fetch(`${apiBase}/schedule/check_usage_status/?${params}`)
    if (!res.ok) return null
    const data = await res.json()
    return typeof data?.usage_status === 'number' ? data.usage_status : null
  } catch {
    return null
  }
}

async function applyQrSchedule(item: ScheduleItem) {
  qrSchedule.value = item
  qrUrl.value = buildQrUrl(item)
  await renderQr(qrUrl.value)

  const sourceType = item.objective === 'schedule' ? 'schedule' : 'booking'
  const usageStatus = await checkUsageStatus(String(item.rowId), sourceType)
  usageClosedMsg.value = usageStatus !== null && usageStatus >= 3
    ? 'ห้องดังกล่าวได้ปรับเปลี่ยนสถานะแล้วไม่สามารถเปิดใช้งานซ้ำซ้อนได้'
    : ''
}

async function copyUrl() {
  if (!qrUrl.value) return
  await navigator.clipboard.writeText(qrUrl.value)
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

// ---------------------------------------------------------------------------
// REST API polling (right pane) + auto QR selection (left pane)
// ---------------------------------------------------------------------------
function timeToMinutes(t: string): number {
  const [h, m] = t.split(':').map(Number)
  return h * 60 + m
}

function isItemActive(item: ScheduleItem): boolean {
  if (!item.startTime || !item.finishTime) return false
  const now = new Date()
  const nowMin = now.getHours() * 60 + now.getMinutes()
  return timeToMinutes(item.startTime) <= nowMin && nowMin <= timeToMinutes(item.finishTime)
}

function findActiveItem(items: ScheduleItem[]): ScheduleItem | null {
  return items.find(isItemActive) ?? null
}

function syncActiveItem() {
  const active = findActiveItem(scheduleItems.value)
  if (active) {
    if (active.id !== activeItemId.value) {
      activeItemId.value = active.id
      applyQrSchedule(active)
    }
  } else {
    activeItemId.value = ''
    qrSchedule.value = null
    usageClosedMsg.value = ''
    staffAccessLink.value = staffAccessUrl ? `${staffAccessUrl}?room=${encodeURIComponent(roomNo)}` : ''
    qrUrl.value = staffAccessLink.value
    if (staffAccessLink.value) renderQr(staffAccessLink.value)
  }
}

async function setComplete(uuid: string) {
  const params = new URLSearchParams({ uuid })
  const res = await fetch(`${apiBase}/room-usage/set_complete/?${params}`)
  const data = await res.json()
  return data as { Flag: boolean }
}

// async function setUsageStatus(scheduleId: string, status: 0 | 1 | 2 | 3) {
//   const params = new URLSearchParams({ schedule_id: scheduleId, status: String(status) })
//   const res = await fetch(`${apiBase}/schedule/set_schedule_usage_status/?${params}`)
//   const data = await res.json()
//   return data as { success: boolean; schedule_id: string; status: number }
// }

async function updateStatus() {
  // status: 0=allowupdate, 1=confirmed, 2=allow change, 3=complete
  const now = nowMinutes.value
  for (const item of scheduleItems.value) {
    if (!item.startTime) continue
    if (now >= timeToMinutes(item.startTime) + 20 && item.status === 0) item.status = 2
    if (item.finishTime && now > timeToMinutes(item.finishTime) && item.status === 1) {
      const resultComplete = await setComplete(item.uuid)
      if (resultComplete.Flag === true) {
        item.status = 3
      }
    }
  }
}

async function fetchSchedule() {
  if (!apiBase || !roomNo) return
  if (scheduleItems.value.length > 0) { updateStatus(); syncActiveItem(); return }
  try {
    const url = `${apiBase}/schedule/get_current_schedule_DB/?roomCode=${roomNo}`
    //console.log(url);
    const res = await fetch(url)
    if (!res.ok) return
    const data: ScheduleItem[] = await res.json()
    scheduleItems.value = (Array.isArray(data) ? data : []).map(item => ({
      ...item,
      uuid:       item.uuid ?? crypto.randomUUID(),
      startTime:  item.startTime  ?? item.timeperiodfrom,
      finishTime: item.finishTime ?? item.timeperiodto,
      status:     item.status     ?? 0,
    }))

    await syncActiveItem()
    await updateStatus()
  } catch {
    // silent — show stale data rather than an error flash
  }
}

// ---------------------------------------------------------------------------
// MQTT broker status check
// ---------------------------------------------------------------------------
async function checkBrokerStatus(): Promise<boolean> {
  try {
    const res = await fetch(`${apiBase}/send-mqtt/status`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    return data?.status === 'ok'
  } catch (err) {
    errorMessage.value = `Broker status check failed: ${(err as Error).message}`
    return false
  }
}

// ---------------------------------------------------------------------------
// Shared handlers — used by BOTH the SSE path (connectMqttStream) and the
// direct-broker path (handleMessage) so state-changing logic lives in
// exactly one place instead of being duplicated across two code paths.
// ---------------------------------------------------------------------------


async function handleStaffAccess(
  msgRoom: string | undefined,
  msgStatus: string | undefined,
  msgSubjectCode?: string | undefined,
  msgUserName?: string | undefined,
) {
  debugLog(`[staff_access] room=${msgRoom} status=${msgStatus} (this room=${roomNo})`)
  if (msgRoom && msgRoom !== roomNo) {
    debugLog('[staff_access] ignored — room mismatch')
    return
  }
  if (msgStatus === 'on') {

    staffUsingRoom.value = true
    staffSubjectCode.value = msgSubjectCode ?? ''
    staffUserName.value = msgUserName ?? ''
  } else if (msgStatus === 'off') {
    staffUsingRoom.value = false
    window.location.reload()
  }
}

async function handleStaffAccessUpdateStatus(msgRoom: string | undefined, msgStatus: string | undefined) {
  debugLog(`[staff_access/update_status] room=${msgRoom} status=${msgStatus} (this room=${roomNo})`)
  if (msgRoom && msgRoom !== roomNo) {
    debugLog('[staff_access/update_status] ignored — room mismatch')

    staffAccessTimer = setInterval(() => { staffAccessSeconds.value += 1 }, 1000)
    // .panes (and its QR canvas) is v-if-gated on staffUsingRoom, so the
    // close-QR canvas only exists in the DOM after this reactive update flushes.
    await renderCloseQr()
    return
  }
  if (msgStatus === 'off' && staffUsingRoom.value) {
    staffUsingRoom.value = false
  }
}

async function handleTogglePopup(msgRoom: string | undefined, msgStatus: string | undefined) {
  debugLog(`[toggle_popup] room=${msgRoom} status=${msgStatus} (this room=${roomNo})`)
  if (msgRoom && msgRoom !== roomNo) {
    debugLog('[toggle_popup] ignored — room mismatch')
    return
  }
  staffUsingRoom.value = msgStatus === 'on'
  if (msgStatus === 'on') {
    staffAccessSeconds.value = 0
    if (staffAccessTimer) clearInterval(staffAccessTimer)
    staffAccessTimer = setInterval(() => { staffAccessSeconds.value += 1 }, 1000)
    // .panes (and its QR canvas) is v-if-gated on staffUsingRoom, so the
    // close-QR canvas only exists in the DOM after this reactive update flushes.
    await renderCloseQr()
  } else {
    if (staffAccessTimer) {
      clearInterval(staffAccessTimer)
      staffAccessTimer = null
    }
    window.location.reload()
  }
}

async function handleSubmitClose(msgRoom: string | undefined) {
  debugLog(`[submit_close] room=${msgRoom} (this room=${roomNo})`)
  if (msgRoom === roomNo && staffUsingRoom.value) {
    staffUsingRoom.value = false
  }
}

function handlePersonCount(msgRoom: string | undefined, msgCount: number | undefined) {
  debugLog(`[person_count] room=${msgRoom} count=${msgCount} (this room=${roomNo})`)
  if (msgRoom && msgRoom !== roomNo) {
    debugLog('[person_count] ignored — room mismatch')
    return
  }
  if (typeof msgCount === 'number') personCount.value = msgCount
}

async function handleScheduleUpdateEvent(status: string | undefined, updateValue: string | undefined, shouldRefresh: boolean) {
  lastUpdate.value = { status: status ?? '', update: updateValue ?? '' }
  debugLog(`[schedule-update] status=${status} update=${updateValue} refresh=${shouldRefresh}`)
  if (shouldRefresh) {
    scheduleItems.value = []
    qrSchedule.value = null
    await fetchSchedule()
  }
}

// ---------------------------------------------------------------------------
// MQTT handling (left pane QR) — direct browser-to-broker connection
// ---------------------------------------------------------------------------
async function handleMessage(topic: string, payloadBuf: Uint8Array) {
  const raw = new TextDecoder().decode(payloadBuf).trim()
  //debugLog(`[mqtt] message topic=${topic} payload=${raw}`)
  lastTopic.value = topic

 // console.log(`[mqtt] message topic=${topic} payload=${raw}`)

  try {
    const parsed = JSON.parse(raw)

    if (topic === 'update_schedule') {
      const status = parsed?.status as string
      const update_time = parsed?.update as string
      await handleScheduleUpdateEvent(status, update_time, status === 'replace' || status === 'delete')
      return
    }

    if (topic === 'mq_update_schedule') {
      const status = parsed?.status as string
      const date = parsed?.date as string
      await handleScheduleUpdateEvent(status, date, status === 'booking_schedule' || status === 'cancel_schedule')
      return
    }


    if (topic === 'toggle_popup/popup') {
     //console.log('toggle_popup/popup',"0000XXXXX"+parsed)
      handleTogglePopup(parsed?.room_no as string | undefined, parsed?.status as string | undefined)
      return
    }

    if (topic.startsWith('staff_access/')) {
      handleStaffAccess(
        parsed?.room_no as string | undefined,
        parsed?.status as string | undefined,
        parsed?.subject_code as string | undefined,
        parsed?.user_name as string | undefined,
      )
      return
    }

    const item: ScheduleItem = Array.isArray(parsed) ? parsed[0] : parsed
    if (item?.id && item?.coursecode) applyQrSchedule(item)
  } catch {
    errorMessage.value = `Invalid JSON on ${topic}: ${raw}`
    //debugLog(`[mqtt] invalid JSON on topic=${topic} payload=${raw}`)
  }
}



// ---------------------------------------------------------------------------
// Backend SSE stream (control-api /mqtt-stream/subscribe).
// This connection has proven more reliable in practice (confirmed via
// server logs) than the direct broker connection below, so it now also
// drives app state via the same shared handlers — not just a debug mirror.
// ---------------------------------------------------------------------------
function connectMqttStream() {
  const url = `${apiBase}/mqtt-stream/subscribe`
  //debugLog(`[mqtt-stream] connecting to ${url}`)
  mqttStreamSource = new EventSource(url)

  mqttStreamSource.onopen = () => {
    debugLog('[mqtt-stream] connected')
  }

  mqttStreamSource.onmessage = async (event) => {
    debugLog(`[mqtt-stream] event: ${event.data}`)
    try {
      const parsed = JSON.parse(event.data)
      if (parsed?.topic) lastStreamTopic.value = parsed.topic

      if (typeof parsed?.topic === 'string' && parsed.topic.startsWith('staff_access/update_status/')) {
        await handleStaffAccessUpdateStatus(parsed.room_no, parsed.status)
        return
      }

      if (typeof parsed?.topic === 'string' && parsed.topic.startsWith('submit_close/')) {
        await handleSubmitClose(parsed.room_no)
        return
      }

      switch (parsed?.kind) {
        case 'staff_access':
          handleStaffAccess(parsed.room_no, parsed.status, parsed.subject_code, parsed.user_name)
          break
        case 'update_schedule':
        case 'mq_update_schedule':
          await handleScheduleUpdateEvent(parsed.status, parsed.update, !!parsed.should_refresh)
          break
        case 'schedule_item':
          if (parsed.item?.id && parsed.item?.coursecode) applyQrSchedule(parsed.item)
          break
        case 'person_count':
          handlePersonCount(parsed.room_no, parsed.count)
          break
        case 'toggle_popup':
          handleTogglePopup(parsed.room_no, parsed.status)
          break
        // "error" / "unknown" kinds are intentionally not applied to state —
        // logged for visibility only.
        default:
          break
      }
    } catch {
      // ignore malformed SSE payload
    }
  }

  mqttStreamSource.onerror = () => {
    debugLog('[mqtt-stream] error / disconnected, browser will auto-retry')
  }
}

// ---------------------------------------------------------------------------
// PWA install prompt (Android/Chrome) — Chrome withholds the automatic
// mini-infobar unless the site calls preventDefault() on this event, so we
// capture it and drive our own "Install" button instead.
// ---------------------------------------------------------------------------
function onBeforeInstallPrompt(e: Event) {
  e.preventDefault()
  deferredInstallPrompt = e as typeof deferredInstallPrompt
  showInstallButton.value = true
  debugLog('[pwa] beforeinstallprompt captured')
}

function onAppInstalled() {
  debugLog('[pwa] app installed')
  showInstallButton.value = false
  deferredInstallPrompt = null
}

async function installApp() {
  if (!deferredInstallPrompt) return
  await deferredInstallPrompt.prompt()
  const { outcome } = await deferredInstallPrompt.userChoice
  debugLog(`[pwa] install prompt outcome: ${outcome}`)
  deferredInstallPrompt = null
  showInstallButton.value = false
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
onMounted(async () => {
  document.body.classList.add('display-view-body')
  window.addEventListener('beforeinstallprompt', onBeforeInstallPrompt)
  window.addEventListener('appinstalled', onAppInstalled)

  const params = new URLSearchParams(window.location.search)
  if (params.get('debug') === '1') localStorage.setItem('displayDebug', '1')
  if (params.get('debug') === '0') localStorage.removeItem('displayDebug')
  debugEnabled = debugEnabled || localStorage.getItem('displayDebug') === '1'
  if (debugEnabled) console.log('[mqtt] debug logging enabled')

  apiBase    = (config.apiRoute ?? 'https://cosai.nrru.ac.th:8000').replace(/\/$/, '')
  bookingUrl = config.bookingUrl ?? ''
  prefixUrl  = config.prefixUrl  ?? ''
  staffAccessUrl = config.staffAccessUrl ?? ''
  roomNo     = new URLSearchParams(window.location.search).get('room') ?? '27.01.01'
  staffAccessLink.value = staffAccessUrl ? `${staffAccessUrl}?room=${encodeURIComponent(roomNo)}` : ''

  // clock
  tickClock()
  clockTimer = setInterval(tickClock, 1_000)

  // show booking QR immediately while the first fetch is in flight
  renderQr(bookingUrl)

  // initial fetch + periodic poll every 5 min
  await fetchSchedule()
  pollTimer = setInterval(fetchSchedule, 5 * 60_000)

  const brokerOk = await checkBrokerStatus()
  if (!brokerOk) status.value = 'error'

  connectMqttStream()

  const { host, port, protocol, path, username, password, reconnectPeriod, connectTimeout } = config.mqtt
  const brokerUrl = `${protocol}://${host}:${port}${path ?? ''}`
  //console.log(brokerUrl);
  const clientId  = 'qr_display_' + Math.random().toString(16).slice(2, 8)
  debugLog(`[mqtt] connecting to ${brokerUrl} clientId=${clientId}`)
  client = mqtt.connect(brokerUrl, {
    clientId,
    username: username || undefined,
    password: password || undefined,
    reconnectPeriod,
    connectTimeout,
  })

  setTimeout(() => {
    const pass = client?.connected ?? false
    debugLog(`[mqtt] connection check: ${pass ? 'PASS' : 'FAIL'} (${brokerUrl})`)
  }, (connectTimeout ?? 10000) + 3000)

  client.on('connect', () => {
    debugLog('[mqtt] connected')
    status.value = 'connected'
    errorMessage.value = ''
    client?.subscribe(config.topics, { qos: 1 }, (err) => {
      if (err) {
        debugLog(`[mqtt] subscribe error: ${(err as Error).message}`)
        errorMessage.value = `Subscribe error: ${(err as Error).message}`
      } else {
        debugLog(`[mqtt] subscribed to: ${config.topics.join(', ')}`)
      }
    })
  })
  client.on('reconnect', () => { debugLog('[mqtt] reconnecting'); status.value = 'connecting' })
  client.on('close',     () => { debugLog('[mqtt] connection closed'); status.value = 'disconnected' })
  client.on('offline',   () => { debugLog('[mqtt] offline') })
  client.on('error',     (err) => { debugLog(`[mqtt] error: ${err.message}`); status.value = 'error'; errorMessage.value = err.message })
  client.on('message', handleMessage)
})

onUnmounted(() => {
  document.body.classList.remove('display-view-body')
  window.removeEventListener('beforeinstallprompt', onBeforeInstallPrompt)
  window.removeEventListener('appinstalled', onAppInstalled)
  client?.end(true)
  mqttStreamSource?.close()
  if (pollTimer)  clearInterval(pollTimer)
  if (clockTimer) clearInterval(clockTimer)
  if (staffAccessTimer) clearInterval(staffAccessTimer)
})
</script>

<template>
  <div class="page">
    <!-- Status bar -->
    <div class="status-bar">
      <span class="dot" :class="status"></span>
      <span class="status-text">{{ status }}</span>
      <span v-if="lastTopic" class="topic">{{ lastTopic }}</span>
      <span v-if="lastStreamTopic" class="topic stream-topic">SSE: {{ lastStreamTopic }}</span>
      <span v-if="lastUpdated" class="timestamp">Updated: {{ lastUpdated }}</span>
      <span v-if="lastUpdate" class="update-badge" :class="lastUpdate.status">
        {{ lastUpdate.status }} · {{ lastUpdate.update }}
      </span>
      <span v-if="personCount !== null" class="person-count-badge">จำนวนผู้ใช้ {{ personCount }} คน</span>
      <a v-if="staffAccessLink" :href="staffAccessLink" target="_blank" class="staff-link">Staff Access</a>
      <button v-if="showInstallButton" class="install-btn" @click="installApp">Install App</button>
    </div>

    <!-- Two-pane layout -->
    <div v-if="!staffUsingRoom" class="panes">

      <!-- LEFT: QR code -->
      <div class="pane pane-left">
        <div class="qr-card">
          <div v-if="rendering" class="qr-spinner"></div>
          <canvas v-show="!rendering" ref="canvasRef"></canvas>
        </div>
        <div v-if="qrUrl" class="qr-url" :class="{ copied }" @click="copyUrl" title="Click to copy">
          <span class="qr-url-text">{{ qrUrl }}</span>
          <span class="qr-url-badge">{{ copied ? 'Copied!' : 'Copy' }}</span>
        </div>
        <div class="qr-clock">{{ currentTime }}</div>
        <template v-if="qrSchedule">
          <p class="qr-hint">Scan to attend</p>
          <div class="qr-meta">
            <span class="qr-meta-time">{{ qrSchedule.startTime }} – {{ qrSchedule.finishTime }}</span>
            <span class="qr-meta-code">{{ qrSchedule.coursecode }}</span>
            <span class="qr-meta-course">{{ qrSchedule.coursename }}</span>
            <span class="qr-meta-teacher">
              {{ qrSchedule.teacher?.[0]?.prefixname }}
              {{ qrSchedule.teacher?.[0]?.officername }}
              {{ qrSchedule.teacher?.[0]?.officersurname }}
            </span>
          </div>
        </template>
        <template v-else-if="!rendering">
          <p class="qr-hint">No active class right now</p>
          <p class="qr-booking-hint">Scan to book this room</p>
        </template>
      </div>

      <!-- RIGHT: schedule list from API -->
      <div class="pane pane-right">
        <p class="section-title">Current Schedule</p>

        <div class="time-grid">
          <!-- period row labels 1–15 -->
          <div
            v-for="row in 15"
            :key="`lbl-${row}`"
            class="period-label"
            :style="{ gridRow: row, gridColumn: 1 }"
          >{{ row }}</div>

          <!-- row separators -->
          <div
            v-for="row in 15"
            :key="`sep-${row}`"
            class="period-sep"
            :style="{ gridRow: row, gridColumn: 2 }"
          ></div>

          <!-- schedule cards spanning periods -->
          <div
            v-for="item in scheduleItems"
            :key="item.id"
            class="sched-card"
            :class="{ active: item.id === activeItemId }"
            :style="{
              gridRow: `${item.timeperiodfrom} / ${Number(item.timeperiodto) + 1}`,
              gridColumn: 2
            }"
          >
            <div class="card-row card-row-header">
              <span class="card-value mono">{{ item.startTime }} – {{ item.finishTime }}</span>
              <span class="status-badge" :class="getItemStatus(item)">{{ getItemStatus(item) }}</span>
            </div>
            <div class="card-row">
              <span class="card-label">Code</span>
              <span class="card-value mono">{{ item.coursecode }}</span>
            </div>
            <div class="card-row course-row">
              <span class="card-label">Course</span>
              <span class="card-value course-name">{{ item.coursename }}</span>
            </div>
            <div class="divider"></div>
            <div v-for="t in item.teacher" :key="t.officerid" class="teacher-row">
              <span class="teacher-name">{{ t.prefixname }} {{ t.officername }} {{ t.officersurname }}</span>
            </div>
          </div>

          <p v-if="!scheduleItems.length" class="placeholder grid-placeholder">No active classes</p>
        </div>
      </div>

    </div>

    <p v-if="errorMessage" class="error">{{ errorMessage }}</p>

    <!-- Usage already closed/completed — blocks re-opening a stale QR -->
    <div v-if="usageClosedMsg" class="usage-closed-overlay">
      <div class="usage-closed-card">
        <p class="usage-closed-text">{{ usageClosedMsg }}</p>
      </div>
    </div>

    <!-- Staff-using-room overlay -->
    <div v-if="staffUsingRoom" class="staff-overlay">
      <div class="staff-overlay-title">เจ้าหน้าที่กำลังใช้งานห้อง</div>
      <div class="staff-overlay-sub">โปรดรออยู่นอกห้องจนกว่าจะจบการใช้งาน</div>
      <div v-if="staffUserName || staffSubjectCode" class="staff-overlay-info">
        <span v-if="staffUserName">{{ staffUserName }}</span>
        <span v-if="staffSubjectCode"> · {{ staffSubjectCode }}</span>
      </div>
      <div class="staff-overlay-timer">{{ formatElapsed(staffAccessSeconds) }}</div>
      <div class="staff-overlay-qr">
        <canvas ref="closeQrCanvasRef"></canvas>
      </div>
      <div class="staff-overlay-qr-hint">Scan to end access early</div>
      <div v-if="closeUrl" class="staff-overlay-url" :class="{ copied: closeCopied }" @click="copyCloseUrl" title="Click to copy">
        <span class="staff-overlay-url-text">{{ closeUrl }}</span>
        <span class="staff-overlay-url-badge">{{ closeCopied ? 'Copied!' : 'Copy' }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
* { box-sizing: border-box; }

:global(body.display-view-body) {
  margin: 0;
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  background: #111827;
}

.page {
  width: 98vw;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #111827;
  color: #f9fafb;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  overflow: hidden;
  margin: 0 auto;
}

/* Status bar */
.status-bar {
  position: fixed;
  bottom: 0.75rem;
  right: 1rem;
  display: flex;
  align-items: center;
  gap: 0.6rem;
  font-size: 0.75rem;
  color: #6b7280;
  z-index: 10;
  background: rgba(17, 24, 39, 0.7);
  padding: 0.25rem 0.6rem;
  border-radius: 999px;
  backdrop-filter: blur(4px);
}
.update-badge { padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.7rem; font-weight: 600; }
.update-badge.replace { background: rgba(59,130,246,.2); color: #60a5fa; }
.update-badge.delete  { background: rgba(239,68,68,.2);  color: #f87171; }
.person-count-badge {
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 600;
  background: rgba(34, 197, 94, .2);
  color: #4ade80;
}
.dot { width: 9px; height: 9px; border-radius: 50%; background: #6b7280; flex-shrink: 0; }
.dot.connected    { background: #22c55e; }
.dot.connecting   { background: #eab308; }
.dot.disconnected,
.dot.error        { background: #ef4444; }
.topic { font-family: monospace; }
.stream-topic { color: #60a5fa; }
.staff-link { color: #60a5fa; text-decoration: none; }
.staff-link:hover { text-decoration: underline; }

.install-btn {
  background: #22c55e;
  color: #052e16;
  border: none;
  border-radius: 999px;
  padding: 0.15rem 0.65rem;
  font-size: 0.7rem;
  font-weight: 700;
  cursor: pointer;
}
.install-btn:hover { background: #4ade80; }

/* Panes */
.panes {
  flex: 1;
  display: flex;
  flex-direction: row;
  padding: 1.5rem 2rem 2rem;
  gap: 2rem;
  overflow: hidden;
}

.pane {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.pane-left  { flex: 0 0 30%; min-width: 0; align-items: stretch; }
.pane-right { flex: 0 0 70%; }

/* LEFT */
.qr-card {
  background: #ffffff;
  padding: 1.25rem;
  border-radius: 1rem;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  aspect-ratio: 1 / 1;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4);
  box-sizing: border-box;
}
.qr-card canvas {
  width: 100% !important;
  height: 100% !important;
  object-fit: contain;
}
.qr-spinner {
  width: 56px;
  height: 56px;
  border: 5px solid rgba(0, 0, 0, 0.12);
  border-top-color: #374151;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.qr-url {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.6rem;
  padding: 0.35rem 0.75rem;
  background: #1f2937;
  border: 1px solid #374151;
  border-radius: 0.5rem;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
  width: 100%;
  overflow: hidden;
}
.qr-url:hover { border-color: #4b5563; background: #263040; }
.qr-url.copied { border-color: #22c55e; background: #052e16; }
.qr-url-text {
  flex: 1;
  font-size: 0.65rem;
  font-family: monospace;
  color: #9ca3af;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.qr-url-badge {
  flex-shrink: 0;
  font-size: 0.6rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #4b5563;
  transition: color 0.2s;
}
.qr-url.copied .qr-url-badge { color: #4ade80; }

.qr-clock {
  margin-top: 1rem;
  font-size: 2rem;
  font-weight: 700;
  font-family: monospace;
  color: #f9fafb;
  letter-spacing: 0.05em;
}
.qr-hint {
  margin-top: 1rem;
  font-size: 0.9rem;
  color: #6b7280;
  letter-spacing: 0.05em;
}
.qr-booking-hint {
  font-size: 0.8rem;
  color: #4b5563;
  letter-spacing: 0.04em;
  margin-top: 0.25rem;
}
.qr-meta {
  margin-top: 0.75rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.2rem;
  text-align: center;
}
.qr-meta-time {
  font-family: monospace;
  font-size: 1rem;
  color: #22c55e;
  font-weight: 600;
}
.qr-meta-code {
  font-family: monospace;
  font-size: 0.8rem;
  color: #6b7280;
}
.qr-meta-course {
  font-size: 1.1rem;
  font-weight: 600;
  color: #f9fafb;
}
.qr-meta-teacher {
  font-size: 0.9rem;
  color: #9ca3af;
}

/* RIGHT */
.pane-right {
  align-items: stretch;
  justify-content: flex-start;
  padding-left: 1.5rem;
  border-left: 1px solid #1f2937;
  overflow: hidden;
}

.section-title {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: #4b5563;
  margin-bottom: 0.5rem;
  flex-shrink: 0;
}

.time-grid {
  display: grid;
  grid-template-columns: 1.75rem 1fr;
  grid-template-rows: repeat(15, 1fr);
  width: 100%;
  height: 100%;
  gap: 2px 0.5rem;
}

.period-label {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  font-size: 0.6rem;
  font-family: monospace;
  color: #374151;
  padding-right: 0.3rem;
  user-select: none;
}

.period-sep {
  border-top: 1px dashed #1f2937;
  pointer-events: none;
}

.grid-placeholder {
  grid-row: 1 / 16;
  grid-column: 2;
}

.sched-card {
  background: #1f2937;
  border: 1px solid #374151;
  border-radius: 0.75rem;
  padding: 1rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  transition: border-color 0.3s, background 0.3s;
}
.sched-card.active {
  background: #052e16;
  border-color: #22c55e;
  box-shadow: 0 0 0 1px #22c55e, 0 4px 20px rgba(34, 197, 94, 0.2);
}

.card-row {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
}
.card-row-header { align-items: center; }
.status-badge {
  margin-left: auto;
  font-size: 0.6rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 0.2rem 0.5rem;
  border-radius: 999px;
  flex-shrink: 0;
}
.status-badge.active    { background: #14532d; color: #4ade80; border: 1px solid #22c55e; }
.status-badge.upcoming  { background: #1e3a5f; color: #60a5fa; border: 1px solid #3b82f6; }
.status-badge.finished  { background: #1f2937; color: #6b7280; border: 1px solid #374151; }
.status-badge.confirmed { background: #422006; color: #fbbf24; border: 1px solid #f59e0b; }
.status-badge.closed    { background: #450a0a; color: #f87171; border: 1px solid #ef4444; }
.card-label {
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: #4b5563;
  width: 3.5rem;
  flex-shrink: 0;
}
.card-value {
  font-size: 1rem;
  font-weight: 600;
  color: #f9fafb;
}
.course-row { align-items: flex-start; }
.course-name {
  font-size: 1.15rem;
  line-height: 1.4;
}
.mono { font-family: monospace; }

.divider {
  height: 1px;
  background: #374151;
  margin: 0.2rem 0;
}

.teacher-row { display: flex; align-items: center; }
.teacher-name {
  font-size: 0.95rem;
  color: #d1d5db;
}

.placeholder {
  color: #374151;
  font-size: 1.4rem;
  font-weight: 700;
  font-style: italic;
  text-align: center;
  align-self: center;
  margin: auto;
  letter-spacing: 0.03em;
}

.error {
  position: fixed;
  bottom: 1rem;
  left: 50%;
  transform: translateX(-50%);
  color: #f87171;
  font-size: 0.85rem;
  background: #1f2937;
  padding: 0.4rem 1rem;
  border-radius: 0.5rem;
}

.staff-overlay {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  padding: 2rem 2.5rem;
  width: 100vw;
  height: 100vh;
  overflow-y: auto;
  background: #1f2937;
  text-align: center;
}
.staff-overlay-title {
  font-size: 1.6rem;
  font-weight: 800;
  color: #fbbf24;
  letter-spacing: 0.02em;
}
.staff-overlay-sub {
  font-size: 0.95rem;
  font-weight: 500;
  color: #cbd5e1;
}
.staff-overlay-info {
  font-size: 1rem;
  font-weight: 600;
  color: #fbbf24;
}
.staff-overlay-timer {
  font-family: monospace;
  font-size: 1.75rem;
  font-weight: 700;
  color: #f9fafb;
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid rgba(245, 158, 11, 0.35);
  padding: 0.3rem 1rem;
  border-radius: 0.6rem;
}
.staff-overlay-qr {
  background: #ffffff;
  padding: 1.25rem;
  border-radius: 1.25rem;
  line-height: 0;
}
.staff-overlay-qr canvas {
  width: min(32vh, 32vw, 20rem) !important;
  height: min(32vh, 32vw, 20rem) !important;
}
.staff-overlay-qr-hint {
  font-size: 0.85rem;
  color: #94a3b8;
}
.staff-overlay-url {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.85rem;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid #374151;
  border-radius: 0.5rem;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
  max-width: 100%;
  overflow: hidden;
}
.staff-overlay-url:hover { border-color: #4b5563; }
.staff-overlay-url.copied { border-color: #22c55e; background: rgba(34, 197, 94, 0.2); }
.staff-overlay-url-text {
  font-size: 0.7rem;
  font-family: monospace;
  color: #9ca3af;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.staff-overlay-url-badge {
  flex-shrink: 0;
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #6b7280;
}
.staff-overlay-url.copied .staff-overlay-url-badge { color: #4ade80; }

.usage-closed-overlay {
  position: fixed;
  inset: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem;
  background: rgba(15, 23, 42, 0.85);
  backdrop-filter: blur(2px);
}
.usage-closed-card {
  max-width: 480px;
  width: 100%;
  padding: 2rem 2.25rem;
  border-radius: 1rem;
  background: #1f2937;
  border: 1px solid #f59e0b;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
  text-align: center;
}
.usage-closed-text {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 700;
  color: #fbbf24;
  line-height: 1.5;
}
</style>
