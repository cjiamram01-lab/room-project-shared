<script setup lang="ts">
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import config from "../assets/config.json";
import { locale } from "../i18n";
import { useUserStore } from "../stores/user";

type LoadState = "loading" | "ready" | "error";
type ApiStatus = "checking" | "online" | "offline";
type RoomTone = "free" | "busy" | "scheduled" | "unknown";
type OverviewView = "calendar" | "rooms";

interface RoomSlot {
  roomcode: string;
  startTime: string;
  finishTime: string;
  usageStatus?: string | number;
}

interface DashboardPayload {
  schedule_count: number;
  usage_count: number;
  all_rooms_count: number;
  free_rooms_count: number;
  list_empty_rooms: RoomSlot[];
  list_usage_rooms: RoomSlot[];
}

interface RoomImageRecord {
  image: string;
}

interface RoomRecord {
  id?: number;
  room_no: string;
  panorama?: string | null;
  room_type?: string | null;
  floor_no?: number | null;
  building?: string | null;
  computer_no?: number | null;
  seat_no?: number | null;
  images?: RoomImageRecord[];
}

interface FeatureRecord {
  code?: string;
  application?: string;
  accessory?: string;
  icon?: string;
  usage?: boolean;
}

interface TeacherRecord {
  prefixname?: string;
  officername?: string;
  officersurname?: string;
  officerlogin?: string;
}

interface ScheduleRecord {
  rowId?: number;
  id?: string | number;
  schedule_id?: string | number;
  roomcode: string;
  coursecode?: string;
  coursename?: string;
  subject_code?: string;
  subject_name?: string;
  schedule_date: string;
  teacher_name?: string | TeacherRecord[];
  userCode?: string;
  user_code?: string;
  user_name?: string;
  user_login?: string;
  startTime: string;
  finishTime: string;
  objective?: string;
  source?: string;
  usage_status?: number | string;
}

interface RoomStatusView {
  roomcode: string;
  floorNo: number | null;
  tone: RoomTone;
  label: string;
  context: string;
  subjectName?: string;
  instructorName?: string;
  bookingStartTime?: string;
  bookingFinishTime?: string;
}

interface RoomStatusGroup {
  key: string;
  floorNo: number | null;
  rooms: RoomStatusView[];
}

interface CalendarDay {
  date: string;
  dayNo: number;
  currentMonth: boolean;
  isToday: boolean;
  isSelected: boolean;
  events: ScheduleRecord[];
}

interface PersonCountEvent {
  kind: string;
  room_no: string;
  count: number;
}

const API_BASE = (config.apiRoute ?? "http://localhost:8000/").replace(/\/$/, "");
const router = useRouter();
const userStore = useUserStore();
const showBooking = inject<(startTime?: string, finishTime?: string, roomCode?: string, bookingDate?: string) => void>("showBooking");
const dashboard = ref<DashboardPayload | null>(null);
const rooms = ref<RoomRecord[]>([]);
const schedules = ref<ScheduleRecord[]>([]);
const applications = ref<FeatureRecord[]>([]);
const accessories = ref<FeatureRecord[]>([]);
const personCounts = ref(new Map<string, number>());
const state = ref<LoadState>("loading");
const errorMessage = ref("");
const apiStatus = ref<ApiStatus>("checking");
const apiStatusDetail = ref("");
const apiCheckedAt = ref<Date | null>(null);
const apiModalDismissed = ref(false);
const apiRetrying = ref(false);
const detailLoading = ref(false);
const detailError = ref("");
const selectedRoomCode = ref("");
const selectedRoomDetail = ref<RoomRecord | null>(null);
const selectedGalleryIndex = ref(0);
const roomDetailDialog = ref<HTMLDialogElement | null>(null);
const lightboxDialog = ref<HTMLDialogElement | null>(null);
const datePickerInput = ref<HTMLInputElement | null>(null);
const lightboxImageFailed = ref(false);
const selectedDate = ref(localDateKey(new Date()));
const activeOverviewView = ref<OverviewView>("rooms");
const loginPromptVisible = ref(false);
const visibleMonth = ref(selectedDate.value.slice(0, 7));
const now = ref(new Date());
const imageFailed = ref(false);
const lastUpdatedAt = ref<Date | null>(null);
const dayButtonRefs = new Map<string, HTMLButtonElement>();
const weekdays = [
  { short: "อา", full: "อาทิตย์" },
  { short: "จ", full: "จันทร์" },
  { short: "อ", full: "อังคาร" },
  { short: "พ", full: "พุธ" },
  { short: "พฤ", full: "พฤหัสบดี" },
  { short: "ศ", full: "ศุกร์" },
  { short: "ส", full: "เสาร์" },
];
let clockTimer: ReturnType<typeof setInterval> | null = null;
let dashboardRefreshTimer: ReturnType<typeof setInterval> | null = null;
let apiCheckTimer: ReturnType<typeof setInterval> | null = null;
let personCountStream: EventSource | null = null;
let detailRequestGeneration = 0;

function localDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function toMinutes(time: string): number {
  const [hour = 0, minute = 0] = time.split(":").map(Number);
  return hour * 60 + minute;
}

function sameRoom(left: string, right: string): boolean {
  return left.trim().toLowerCase() === right.trim().toLowerCase();
}

function roomCodeOf(raw: Record<string, unknown>): string {
  const value = raw.room_no ?? raw.roomNo ?? raw.room_code ?? raw.roomCode ?? raw.roomcode;
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

function numberOf(raw: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = raw[key];
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) return Number(value);
  }
  return null;
}

function stringOf(raw: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = raw[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return null;
}

function normalizeRooms(payload: unknown): RoomRecord[] {
  const rows = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object" && Array.isArray((payload as { data?: unknown }).data)
      ? (payload as { data: unknown[] }).data
      : [];

  return rows.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const raw = row as Record<string, unknown>;
    const roomNo = roomCodeOf(raw);
    if (!roomNo) return [];
    return [{
      id: numberOf(raw, ["id"]) ?? undefined,
      room_no: roomNo,
      panorama: stringOf(raw, ["panorama"]),
      room_type: stringOf(raw, ["room_type", "roomType", "type"]),
      floor_no: numberOf(raw, ["floor_no", "floorNo", "floor"]),
      building: stringOf(raw, ["building", "building_name", "buildingName"]),
      computer_no: numberOf(raw, ["computer_no", "computerNo", "computers", "computer_count"]),
      seat_no: numberOf(raw, ["seat_no", "seatNo", "seats", "seat_count"]),
      images: Array.isArray(raw.images)
        ? raw.images.flatMap((item) => {
            if (!item || typeof item !== "object") return [];
            const image = stringOf(item as Record<string, unknown>, ["image"]);
            return image ? [{ image }] : [];
          })
        : [],
    }];
  });
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

async function checkApiConnection() {
  // /test/ping touches neither MySQL nor the MQTT broker, so a failure here
  // means the API itself is unreachable rather than one of its dependencies
  // being down — which is the difference the banner needs to report.
  const controller = new AbortController();
  // fetch() has no timeout of its own: without this, an unreachable server
  // leaves the request hanging and the banner never appears.
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(`${API_BASE}/test/ping`, { signal: controller.signal });
    // Only a 5xx counts as broken. Any other reply — including a 404 from an
    // older control-api build that has no /test/ping — proves the server is
    // reachable and answering, which is all this check is asking.
    if (response.status >= 500) {
      apiStatus.value = "offline";
      apiStatusDetail.value = `เซิร์ฟเวอร์ตอบกลับรหัส ${response.status}`;
      return false;
    }
    apiStatus.value = "online";
    apiStatusDetail.value = "";
    // Armed again, so a later outage raises the modal instead of being
    // silently downgraded to the banner the user dismissed hours ago.
    apiModalDismissed.value = false;
    return true;
  } catch (error) {
    apiStatus.value = "offline";
    apiStatusDetail.value = (error as Error)?.name === "AbortError"
      ? "เซิร์ฟเวอร์ไม่ตอบสนองภายใน 5 วินาที"
      : "ไม่สามารถเชื่อมต่อกับเซิร์ฟเวอร์ได้";
    return false;
  } finally {
    clearTimeout(timeout);
    apiCheckedAt.value = new Date();
  }
}

async function retryConnection() {
  if (apiRetrying.value) return;
  // apiStatus stays "offline" for the duration so the modal does not flicker
  // out and back in while the ping is in flight.
  apiRetrying.value = true;
  try {
    if (await checkApiConnection()) {
      await loadCoreData();
      // The EventSource died with the server it was attached to; a fresh one is
      // needed or live person counts stay frozen after the API comes back.
      resubscribePersonCount();
    }
  } finally {
    apiRetrying.value = false;
  }
}

function dismissApiModal() {
  apiModalDismissed.value = true;
}

function handleApiModalKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") dismissApiModal();
}

// Unmissable while the dashboard is untrustworthy; once dismissed it steps
// down to the banner, so the warning never disappears entirely.
const showApiModal = computed(() => apiStatus.value === "offline" && !apiModalDismissed.value);
const showApiBanner = computed(() => apiStatus.value === "offline" && apiModalDismissed.value);
const apiCheckedAtLabel = computed(() => apiCheckedAt.value
  ? apiCheckedAt.value.toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
  : "");

// The listener goes on window, not on the overlay: keydown only reaches an
// element that has focus, and the overlay div is not focusable, so Esc bound
// there would do nothing unless focus happened to be inside already.
watch(showApiModal, (visible) => {
  if (visible) window.addEventListener("keydown", handleApiModalKeydown);
  else window.removeEventListener("keydown", handleApiModalKeydown);
});

function normalizeDashboard(payload: unknown): DashboardPayload {
  const raw = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  return {
    schedule_count: numberOf(raw, ["schedule_count"]) ?? 0,
    usage_count: numberOf(raw, ["usage_count"]) ?? 0,
    all_rooms_count: numberOf(raw, ["all_rooms_count"]) ?? 0,
    free_rooms_count: numberOf(raw, ["free_rooms_count"]) ?? 0,
    list_empty_rooms: Array.isArray(raw.list_empty_rooms) ? raw.list_empty_rooms as RoomSlot[] : [],
    list_usage_rooms: Array.isArray(raw.list_usage_rooms) ? raw.list_usage_rooms as RoomSlot[] : [],
  };
}

async function loadCoreData(showLoading = true) {
  if (showLoading) state.value = "loading";
  errorMessage.value = "";
  const [dashboardResult, roomsResult, schedulesResult] = await Promise.allSettled([
    fetchJson<unknown>(`${API_BASE}/schedule/get_rooms_dashboard/`),
    fetchJson<unknown>(`${API_BASE}/room/get_all_rooms`),
    fetchJson<unknown>(`${API_BASE}/schedule/get_schedule_from_json/`),
  ]);

  if (dashboardResult.status === "fulfilled") {
    dashboard.value = normalizeDashboard(dashboardResult.value);
    lastUpdatedAt.value = new Date();
  }
  if (roomsResult.status === "fulfilled") rooms.value = normalizeRooms(roomsResult.value);
  if (schedulesResult.status === "fulfilled") {
    schedules.value = Array.isArray(schedulesResult.value) ? schedulesResult.value as ScheduleRecord[] : [];
  }

  // Every request failing points at the API being gone, not at one endpoint
  // misbehaving — catches a server that dies between scheduled pings.
  const results = [dashboardResult, roomsResult, schedulesResult];
  if (results.every((result) => result.status === "rejected")) {
    apiStatus.value = "offline";
    apiStatusDetail.value = "ไม่สามารถเชื่อมต่อกับเซิร์ฟเวอร์ได้";
    apiCheckedAt.value = new Date();
  } else if (apiStatus.value === "offline") {
    apiStatus.value = "online";
    apiStatusDetail.value = "";
  }

  if (dashboardResult.status === "rejected") {
    state.value = "error";
    errorMessage.value = "ไม่สามารถโหลดข้อมูลสถานะห้องแบบเรียลไทม์ได้";
  } else {
    state.value = "ready";
  }

}

async function loadSelectedRoomFeatures(roomCode: string) {
  const requestGeneration = ++detailRequestGeneration;
  applications.value = [];
  accessories.value = [];
  selectedRoomDetail.value = null;
  detailError.value = "";
  detailLoading.value = false;
  if (!roomCode) return;
  detailLoading.value = true;

  const [applicationsResult, accessoriesResult, roomResult] = await Promise.allSettled([
    fetchJson<FeatureRecord[]>(
      `${API_BASE}/application/get_list_application/?room_no=${encodeURIComponent(roomCode)}`,
    ),
    fetchJson<FeatureRecord[]>(
      `${API_BASE}/accessory/get_list_accessory/?room_no=${encodeURIComponent(roomCode)}`,
    ),
    fetchJson<unknown>(`${API_BASE}/room/get_room/${encodeURIComponent(roomCode)}`),
  ]);

  if (requestGeneration !== detailRequestGeneration || !sameRoom(roomCode, selectedRoomCode.value)) return;

  if (applicationsResult.status === "fulfilled" && Array.isArray(applicationsResult.value)) {
    applications.value = applicationsResult.value;
  }
  if (accessoriesResult.status === "fulfilled" && Array.isArray(accessoriesResult.value)) {
    accessories.value = accessoriesResult.value;
  }
  if (roomResult.status === "fulfilled") {
    selectedRoomDetail.value = normalizeRooms([roomResult.value])[0] ?? null;
  }
  if (applicationsResult.status === "rejected" && accessoriesResult.status === "rejected") {
    detailError.value = "ไม่สามารถโหลดข้อมูลซอฟต์แวร์และอุปกรณ์ได้";
  }
  detailLoading.value = false;
}

function resubscribePersonCount() {
  personCountStream?.close();
  personCountStream = null;
  subscribePersonCount();
}

function subscribePersonCount() {
  if (typeof EventSource === "undefined") return;
  personCountStream = new EventSource(`${API_BASE}/mqtt-stream/subscribe/`);
  personCountStream.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data) as PersonCountEvent;
      if (payload.kind !== "person_count" || !payload.room_no || typeof payload.count !== "number") return;
      const next = new Map(personCounts.value);
      next.set(payload.room_no, payload.count);
      personCounts.value = next;
    } catch {
      // Ignore malformed events while preserving the existing SSE connection.
    }
  };
}

const dateLabel = computed(() => now.value.toLocaleDateString("th-TH", {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
}));

const timeLabel = computed(() => now.value.toLocaleTimeString("th-TH", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
}));

const freshnessLabel = computed(() => lastUpdatedAt.value
  ? `อัปเดตล่าสุด ${lastUpdatedAt.value.toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" })} น.`
  : "กำลังเชื่อมต่อข้อมูล");

const nowMinutes = computed(() => now.value.getHours() * 60 + now.value.getMinutes());

const roomStatuses = computed<RoomStatusView[]>(() => {
  const summary = dashboard.value;
  const statusDate = localDateKey(now.value);
  const daySchedules = schedules.value
    .filter((item) => item.schedule_date?.slice(0, 10) === statusDate)
    .sort((left, right) => left.startTime.localeCompare(right.startTime));
  const dayEmptySlots = summary?.list_empty_rooms ?? [];
  const codes = new Set(rooms.value.map((room) => room.room_no));
  daySchedules.forEach((item) => codes.add(item.roomcode));
  dayEmptySlots.forEach((slot) => codes.add(slot.roomcode));
  summary?.list_usage_rooms.forEach((slot) => codes.add(slot.roomcode));

  return [...codes].sort((left, right) => left.localeCompare(right, "th", { numeric: true })).map((roomcode) => {
    const floorFromCode = roomcode.match(/^\d+\.(\d+)\./)?.[1];
    const floorNo = floorFromCode
      ? Number(floorFromCode)
      : (rooms.value.find((room) => sameRoom(room.room_no, roomcode))?.floor_no ?? null);
    const roomSchedules = daySchedules
      .filter((item) => sameRoom(item.roomcode ?? "", roomcode));

    const usageSlots = (summary?.list_usage_rooms ?? [])
      .filter((slot) => sameRoom(slot.roomcode, roomcode))
      .sort((left, right) => left.startTime.localeCompare(right.startTime));
    const activeUsage = usageSlots.find((slot) =>
      nowMinutes.value >= toMinutes(slot.startTime) && nowMinutes.value <= toMinutes(slot.finishTime));
    if (activeUsage) {
      const pending = !Number.isNaN(Number(activeUsage.usageStatus)) && Number(activeUsage.usageStatus) < 3;
      const activeSchedule = roomSchedules.find((item) =>
        toMinutes(item.startTime) === toMinutes(activeUsage.startTime)
        && toMinutes(item.finishTime) === toMinutes(activeUsage.finishTime))
        ?? roomSchedules.find((item) =>
          nowMinutes.value >= toMinutes(item.startTime) && nowMinutes.value <= toMinutes(item.finishTime));
      return {
        roomcode,
        floorNo,
        tone: pending ? "scheduled" : "busy",
        label: pending ? "มีตาราง—ยังไม่ยืนยัน" : "กำลังใช้งาน",
        context: `ถึง ${activeUsage.finishTime} น.`,
        subjectName: activeSchedule ? scheduleCourseName(activeSchedule) : undefined,
        instructorName: activeSchedule ? scheduleOwner(activeSchedule) : undefined,
      };
    }

    const activeFree = dayEmptySlots.find((slot) =>
      sameRoom(slot.roomcode, roomcode)
      && nowMinutes.value >= toMinutes(slot.startTime)
      && nowMinutes.value <= toMinutes(slot.finishTime));
    if (activeFree) {
      return {
        roomcode,
        floorNo,
        tone: "free",
        label: "ว่าง",
        context: `ว่างถึง ${activeFree.finishTime} น.`,
        bookingStartTime: activeFree.startTime,
        bookingFinishTime: activeFree.finishTime,
      };
    }

    const nextUsage = usageSlots.find((slot) => toMinutes(slot.startTime) > nowMinutes.value);
    if (nextUsage) {
      const nextSchedule = roomSchedules.find((item) =>
        toMinutes(item.startTime) === toMinutes(nextUsage.startTime));
      return {
        roomcode,
        floorNo,
        tone: "scheduled",
        label: "มีตารางวันนี้",
        context: `รายการถัดไป ${nextUsage.startTime} น.`,
        subjectName: nextSchedule ? scheduleCourseName(nextSchedule) : undefined,
        instructorName: nextSchedule ? scheduleOwner(nextSchedule) : undefined,
      };
    }

    return { roomcode, floorNo, tone: "unknown", label: "ไม่มีข้อมูลสถานะ", context: "ไม่มีรายการถัดไปวันนี้" };
  });
});

const roomStatusGroups = computed<RoomStatusGroup[]>(() => {
  const groups = new Map<string, RoomStatusGroup>();

  roomStatuses.value.forEach((room) => {
    const key = room.floorNo === null ? "unknown" : String(room.floorNo);
    const group = groups.get(key) ?? { key, floorNo: room.floorNo, rooms: [] };
    group.rooms.push(room);
    groups.set(key, group);
  });

  return [...groups.values()]
    .map((group) => ({
      ...group,
      rooms: group.rooms.sort((left, right) =>
        left.roomcode.localeCompare(right.roomcode, "th", { numeric: true })),
    }))
    .sort((left, right) => {
      if (left.floorNo === null) return 1;
      if (right.floorNo === null) return -1;
      return left.floorNo - right.floorNo;
    });
});

const selectedRoom = computed(() =>
  selectedRoomDetail.value
  ?? rooms.value.find((room) => sameRoom(room.room_no, selectedRoomCode.value))
  ?? null,
);

const selectedRoomStatus = computed(() =>
  roomStatuses.value.find((room) => sameRoom(room.roomcode, selectedRoomCode.value))
  ?? { roomcode: selectedRoomCode.value, floorNo: null, tone: "unknown" as const, label: "ไม่มีข้อมูลสถานะ", context: "" },
);

const roomGalleryImages = computed(() => {
  const room = selectedRoom.value;
  if (!room) return [];

  const gallery: Array<{ src: string; alt: string }> = [];

  const panorama = room.panorama?.trim();
  if (panorama) {
    gallery.push({
      src: /^(https?:|data:|\/)/.test(panorama)
      ? panorama
      : `${API_BASE}/room/get_panorama/${encodeURIComponent(room.room_no)}`,
      alt: `ภาพพาโนรามาห้อง ${room.room_no}`,
    });
  }

  room.images?.forEach((item, index) => {
    const imagePath = item.image?.trim();
    if (!imagePath) return;
    gallery.push({
      src: `${API_BASE}/room/get_room_image/${encodeURIComponent(imagePath)}`,
      alt: `ภาพห้อง ${room.room_no} รูปที่ ${index + 1}`,
    });
  });

  return gallery.filter((item, index, items) =>
    items.findIndex((candidate) => candidate.src === item.src) === index,
  );
});

const selectedGalleryImage = computed(() =>
  roomGalleryImages.value[selectedGalleryIndex.value] ?? roomGalleryImages.value[0] ?? null,
);

const visibleGalleryThumbnails = computed(() => roomGalleryImages.value.slice(0, 4));
const remainingGalleryCount = computed(() => Math.max(0, roomGalleryImages.value.length - 4));

const roomImageUrl = computed(() =>
  imageFailed.value ? "" : selectedGalleryImage.value?.src ?? "",
);

function selectRoomImage(index: number) {
  selectedGalleryIndex.value = index;
  imageFailed.value = false;
}

async function openLightbox(index = selectedGalleryIndex.value) {
  selectRoomImage(index);
  lightboxImageFailed.value = false;
  await nextTick();
  if (lightboxDialog.value && !lightboxDialog.value.open) {
    lightboxDialog.value.showModal();
  }
}

function closeLightbox() {
  lightboxDialog.value?.close();
}

function stepLightbox(direction: number) {
  const total = roomGalleryImages.value.length;
  if (total < 2) return;
  selectRoomImage((selectedGalleryIndex.value + direction + total) % total);
  lightboxImageFailed.value = false;
}

function handleLightboxKeydown(event: KeyboardEvent) {
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    stepLightbox(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    stepLightbox(1);
  }
}

const activeApplications = computed(() =>
  applications.value.filter((item) => item.usage !== false && featureLabel(item)),
);

const activeAccessories = computed(() =>
  accessories.value.filter((item) => item.usage !== false && featureLabel(item)),
);

function featureLabel(item: FeatureRecord): string {
  return (item.application ?? item.accessory ?? item.code ?? "").trim();
}

function featureIconUrl(item: FeatureRecord, type: "application" | "accessory"): string {
  const icon = item.icon?.trim();
  if (!icon) return "";
  if (type === "application" || /^(?:https?:|data:|blob:)/i.test(icon)) return icon;
  return `${API_BASE}/accessory/get_picture/${encodeURIComponent(icon)}`;
}

function hideBrokenFeatureIcon(event: Event) {
  const image = event.currentTarget as HTMLImageElement | null;
  if (image) image.hidden = true;
}

function personCount(roomcode: string): number | undefined {
  return personCounts.value.get(roomcode);
}

function roomAriaLabel(room: RoomStatusView): string {
  const segments = [
    `ดูรายละเอียดห้อง ${room.roomcode}`,
    room.floorNo === null ? "ไม่ระบุชั้น" : `ชั้น ${room.floorNo}`,
    `สถานะ ${room.label}`,
    room.context,
  ];
  if (room.subjectName) segments.push(`วิชา ${room.subjectName}`);
  if (room.instructorName) segments.push(`ผู้สอน ${room.instructorName}`);
  const count = personCount(room.roomcode);
  if (count !== undefined) segments.push(`ตรวจพบผู้ใช้ ${count} คน`);
  return segments.filter(Boolean).join(", ");
}

async function openRoomDetail(roomcode: string) {
  selectedRoomCode.value = roomcode;
  await nextTick();
  if (roomDetailDialog.value && !roomDetailDialog.value.open) {
    roomDetailDialog.value.showModal();
  }
}

function selectRoomSchedule(roomcode: string) {
  selectedRoomCode.value = sameRoom(selectedRoomCode.value, roomcode) ? "" : roomcode;
}

function closeRoomDetail() {
  roomDetailDialog.value?.close();
}

function handleRoomDetailClosed() {
  selectedRoomCode.value = "";
}

function bookRoom(room: RoomStatusView) {
  if (room.tone !== "free" || !room.bookingStartTime || !room.bookingFinishTime) return;
  if (!userStore.isLoggedIn) {
    loginPromptVisible.value = true;
    return;
  }
  showBooking?.(room.bookingStartTime, room.bookingFinishTime, room.roomcode, localDateKey(now.value));
}

function closeLoginPrompt() {
  loginPromptVisible.value = false;
}

function goToLogin() {
  loginPromptVisible.value = false;
  router.push("/login");
}

const roomSchedules = computed(() => activeOverviewView.value === "rooms" && selectedRoomCode.value
  ? schedules.value.filter((item) => sameRoom(item.roomcode ?? "", selectedRoomCode.value))
  : schedules.value,
);

const scheduleDate = computed(() => activeOverviewView.value === "rooms"
  ? localDateKey(now.value)
  : selectedDate.value,
);

const selectedDaySchedules = computed(() => roomSchedules.value
  .filter((item) => item.schedule_date?.slice(0, 10) === scheduleDate.value)
  .sort((left, right) => left.startTime.localeCompare(right.startTime)
    || left.roomcode.localeCompare(right.roomcode, "th", { numeric: true })),
);

const selectedDateScheduleCount = computed(() => schedules.value
  .filter((item) => item.schedule_date?.slice(0, 10) === selectedDate.value)
  .length,
);

const scheduleDateScheduleCount = computed(() => schedules.value
  .filter((item) => item.schedule_date?.slice(0, 10) === scheduleDate.value)
  .length,
);

function addMonths(month: string, delta: number): string {
  const [year, monthIndex] = month.split("-").map(Number);
  const next = new Date(year, monthIndex - 1 + delta, 1);
  return `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}`;
}

function formatMonth(month: string): string {
  const [year, monthIndex] = month.split("-").map(Number);
  return new Date(year, monthIndex - 1, 1).toLocaleDateString("th-TH", { month: "long", year: "numeric" });
}

function formatDate(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  const parsed = new Date(year, month - 1, day);
  return parsed.toLocaleDateString("th-TH", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

const monthLabel = computed(() => formatMonth(visibleMonth.value));
const selectedDateLabel = computed(() => formatDate(selectedDate.value));
const scheduleDateLabel = computed(() => formatDate(scheduleDate.value));

const calendarDays = computed<CalendarDay[]>(() => {
  const [year, monthIndex] = visibleMonth.value.split("-").map(Number);
  const firstOfMonth = new Date(year, monthIndex - 1, 1);
  const start = new Date(firstOfMonth);
  start.setDate(firstOfMonth.getDate() - firstOfMonth.getDay());
  const today = localDateKey(new Date());

  return Array.from({ length: 35 }, (_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    const key = localDateKey(date);
    return {
      date: key,
      dayNo: date.getDate(),
      currentMonth: key.slice(0, 7) === visibleMonth.value,
      isToday: key === today,
      isSelected: key === selectedDate.value,
      events: roomSchedules.value.filter((item) => item.schedule_date?.slice(0, 10) === key),
    };
  });
});

function setDayRef(date: string, element: Element | null) {
  if (element instanceof HTMLButtonElement) dayButtonRefs.set(date, element);
  else dayButtonRefs.delete(date);
}

function selectDate(date: string) {
  selectedDate.value = date;
  visibleMonth.value = date.slice(0, 7);
}

function selectToday() {
  selectDate(localDateKey(new Date()));
}

function openDatePicker() {
  const picker = datePickerInput.value;
  if (!picker) return;
  try {
    picker.showPicker();
  } catch {
    picker.click();
  }
}

function handleDatePickerChange(event: Event) {
  const date = (event.target as HTMLInputElement).value;
  if (date) selectDate(date);
}

async function moveSelectedDate(delta: number) {
  const [year, month, day] = selectedDate.value.split("-").map(Number);
  const next = new Date(year, month - 1, day);
  next.setDate(next.getDate() + delta);
  selectDate(localDateKey(next));
  await nextTick();
  dayButtonRefs.get(selectedDate.value)?.focus();
}

async function moveSelectedMonth(delta: number) {
  const [year, month, day] = selectedDate.value.split("-").map(Number);
  const targetMonth = new Date(year, month - 1 + delta, 1);
  const lastDay = new Date(targetMonth.getFullYear(), targetMonth.getMonth() + 1, 0).getDate();
  const next = new Date(targetMonth.getFullYear(), targetMonth.getMonth(), Math.min(day, lastDay));
  selectDate(localDateKey(next));
  await nextTick();
  dayButtonRefs.get(selectedDate.value)?.focus();
}

function handleDayKeydown(event: KeyboardEvent) {
  const movement: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
  if (event.key in movement) {
    event.preventDefault();
    moveSelectedDate(movement[event.key]);
  } else if (event.key === "PageUp" || event.key === "PageDown") {
    event.preventDefault();
    moveSelectedMonth(event.key === "PageUp" ? -1 : 1);
  }
}

function calendarDayLabel(day: CalendarDay): string {
  const parts = [formatDate(day.date)];
  if (day.isToday) parts.push("วันนี้");
  if (day.isSelected) parts.push("วันที่เลือก");
  parts.push(day.events.length ? `มีรายการ ${day.events.length} รายการ` : "ไม่มีรายการ");
  return parts.join(", ");
}

function scheduleTitle(item: ScheduleRecord): string {
  return scheduleCourseName(item) || item.objective || "รายการจอง";
}

function scheduleCourseName(item: ScheduleRecord): string {
  return item.coursename || item.subject_name || item.coursecode || item.subject_code || "";
}

function scheduleOwner(item: ScheduleRecord): string {
  if (Array.isArray(item.teacher_name)) {
    const teacher = item.teacher_name[0];
    if (teacher) return `${teacher.prefixname ?? ""}${teacher.officername ?? ""} ${teacher.officersurname ?? ""}`.trim();
  }
  if (typeof item.teacher_name === "string") return item.teacher_name;
  return item.user_name || item.user_login || item.userCode || item.user_code || "";
}

function scheduleStatus(item: ScheduleRecord): string {
  const status = Number(item.usage_status);
  if (status === 3) return "กำลังใช้งาน";
  if (status === 4) return "ยกเลิก";
  return "รอเข้าใช้งาน";
}

function scheduleTone(item: ScheduleRecord): string {
  const status = Number(item.usage_status);
  if (status === 3) return "confirmed";
  if (status === 4) return "closed";
  return "scheduled";
}

function isSchedulePast(item: ScheduleRecord): boolean {
  const scheduleDay = item.schedule_date?.slice(0, 10);
  const today = localDateKey(now.value);
  if (!scheduleDay || !item.finishTime) return false;
  if (scheduleDay < today) return true;
  if (scheduleDay > today) return false;
  return toMinutes(item.finishTime) <= nowMinutes.value;
}

watch(selectedRoomCode, (roomCode) => {
  closeLightbox();
  selectedGalleryIndex.value = 0;
  imageFailed.value = false;
  loadSelectedRoomFeatures(roomCode);
});

onMounted(() => {
  selectToday();
  checkApiConnection();
  loadCoreData();
  subscribePersonCount();
  clockTimer = setInterval(() => { now.value = new Date(); }, 1000);
  dashboardRefreshTimer = setInterval(() => loadCoreData(false), 5 * 60_000);
  // Polls far more often than the 5-minute data refresh: this screen runs
  // unattended on the kiosks, so the banner has to clear itself once the API
  // is back without anyone being there to press retry.
  apiCheckTimer = setInterval(async () => {
    const wasOffline = apiStatus.value === "offline";
    if (await checkApiConnection() && wasOffline) {
      await loadCoreData(false);
      resubscribePersonCount();
    }
  }, 30_000);
});

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (dashboardRefreshTimer) clearInterval(dashboardRefreshTimer);
  if (apiCheckTimer) clearInterval(apiCheckTimer);
  window.removeEventListener("keydown", handleApiModalKeydown);
  personCountStream?.close();
  personCountStream = null;
});
</script>

<template>
  <div class="dashboard-test-page">
    <h1 class="sr-only">แดชบอร์ดสถานะและการใช้ห้องคอมพิวเตอร์</h1>

    <div v-if="showApiBanner" class="api-offline-banner" role="alert">
      <span class="api-offline-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <path d="M12 3 1.8 20.4h20.4z" />
          <path d="M12 9.4v4.4" />
          <path d="M12 16.9h.01" />
        </svg>
      </span>
      <div class="api-offline-text">
        <p class="api-offline-title">ไม่สามารถเชื่อมต่อกับระบบได้</p>
        <p class="api-offline-detail">
          {{ apiStatusDetail }}
          <template v-if="apiCheckedAtLabel"> · ตรวจสอบล่าสุด {{ apiCheckedAtLabel }} น.</template>
          · ข้อมูลที่แสดงอาจไม่เป็นปัจจุบัน · ติดต่อผู้ดูแลระบบ
        </p>
      </div>
      <button type="button" class="api-offline-retry" :disabled="apiRetrying" @click="retryConnection">
        {{ apiRetrying ? "กำลังเชื่อมต่อ…" : "ลองเชื่อมต่อใหม่" }}
      </button>
    </div>

    <section class="overview-grid" aria-label="ภาพรวมสถานะห้อง">
      <div class="mobile-current-time">
        <span class="time-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 7.5V12l3 2" />
          </svg>
        </span>
        <div>
          <p class="date-label">{{ dateLabel }}</p>
          <time class="time-label" :datetime="now.toISOString()" aria-label="เวลาปัจจุบัน">{{ timeLabel }}</time>
        </div>
      </div>

      <div class="overview-main">
        <section class="kpi-grid" aria-label="สรุปข้อมูลวันนี้">
          <article class="kpi-card kpi-blue">
            <span class="kpi-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M3 21h18M5 21V7l7-4 7 4v14M9 9h1M14 9h1M9 13h1M14 13h1M9 17h1M14 17h1" /></svg>
            </span>
            <div><p>ห้องทั้งหมด</p><strong>{{ dashboard?.all_rooms_count ?? rooms.length }} <small translate="no">{{ locale === 'en' ? 'rooms' : 'ห้อง' }}</small></strong><span>ข้อมูลห้องในระบบ</span></div>
          </article>
          <article class="kpi-card kpi-green">
            <span class="kpi-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" /><circle cx="12" cy="12" r="9" /></svg>
            </span>
            <div><p>ห้องว่างตอนนี้</p><strong>{{ dashboard?.free_rooms_count ?? 0 }} <small translate="no">{{ locale === 'en' ? 'rooms' : 'ห้อง' }}</small></strong><span>พร้อมใช้งาน</span></div>
          </article>
          <article class="kpi-card kpi-red">
            <span class="kpi-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" /></svg>
            </span>
            <div><p>กำลังใช้งาน</p><strong>{{ dashboard?.usage_count ?? 0 }} <small translate="no">{{ locale === 'en' ? 'rooms' : 'ห้อง' }}</small></strong><span>จากสถานะยืนยัน</span></div>
          </article>
          <article class="kpi-card kpi-amber">
            <span class="kpi-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M7 3v3M17 3v3M4 8h16M5 5h14a1 1 0 0 1 1 1v14H4V6a1 1 0 0 1 1-1Z" /></svg>
            </span>
            <div>
              <p>{{ activeOverviewView === "rooms" ? "ตารางสอนวันนี้" : "ตารางสอนวันที่เลือก" }}</p>
              <strong>{{ scheduleDateScheduleCount }} <small translate="no">{{ locale === 'en' ? (scheduleDateScheduleCount === 1 ? 'entry' : 'entries') : 'รายการ' }}</small></strong>
              <span>{{ activeOverviewView === "rooms" ? "รวมทุกห้องวันนี้" : "รวมทุกห้องในวันนั้น" }}</span>
            </div>
          </article>
        </section>

        <p class="sr-only" aria-live="polite">
          กำลังแสดงมุมมอง {{ activeOverviewView === "calendar" ? "ปฏิทิน" : "สถานะห้องแบบเรียลไทม์" }}
        </p>

        <section
          v-show="activeOverviewView === 'calendar'"
          id="calendar-view-panel"
          class="calendar-panel calendar-panel-expanded"
          :aria-label="`ปฏิทิน ${monthLabel}`"
        >
          <header class="calendar-heading">
            <div class="current-time">
              <span class="time-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="8.5" />
                  <path d="M12 7.5V12l3 2" />
                </svg>
              </span>
              <div>
                <p class="date-label">{{ dateLabel }}</p>
                <time class="time-label" :datetime="now.toISOString()" aria-label="เวลาปัจจุบัน">{{ timeLabel }}</time>
              </div>
            </div>
            <div class="calendar-controls" aria-label="ควบคุมเดือนที่แสดง">
              <button type="button" aria-label="เดือนก่อนหน้า" title="เดือนก่อนหน้า" @click="visibleMonth = addMonths(visibleMonth, -1)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
              </button>
              <span class="calendar-month-picker">
                <button type="button" class="calendar-month-button" aria-label="เลือกวันที่" title="เปิดปฏิทินเลือกวันที่" @click="openDatePicker">
                  {{ monthLabel }}
                </button>
                <input
                  ref="datePickerInput"
                  class="native-date-picker"
                  type="date"
                  :value="selectedDate"
                  aria-label="เลือกวันที่"
                  tabindex="-1"
                  @change="handleDatePickerChange"
                >
              </span>
              <button type="button" aria-label="เดือนถัดไป" title="เดือนถัดไป" @click="visibleMonth = addMonths(visibleMonth, 1)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
              </button>
            </div>
            <div class="calendar-actions">
              <button type="button" class="calendar-today-button" @click="selectToday">วันนี้</button>
              <button
                type="button"
                class="view-switch-button"
                aria-controls="room-status-view-panel"
                @click="activeOverviewView = 'rooms'"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 21h18M5 21V7l7-4 7 4v14M9 9h1M14 9h1M9 13h1M14 13h1M9 17h1M14 17h1" /></svg>
                <span>สถานะห้อง</span>
              </button>
            </div>
          </header>
          <p class="sr-only" aria-live="polite">วันที่เลือก {{ selectedDateLabel }} มี {{ selectedDateScheduleCount }} รายการ</p>
          <div class="calendar-grid" :aria-label="`ปฏิทิน ${monthLabel} ทุกห้อง`">
            <span
              v-for="(weekday, index) in weekdays"
              :key="weekday.full"
              class="weekday"
              :class="{ sunday: index === 0, saturday: index === 6 }"
            >
              <b>{{ weekday.full }}</b>
            </span>
            <button
              v-for="(day, index) in calendarDays"
              :key="day.date"
              :ref="(element) => setDayRef(day.date, element as Element | null)"
              type="button"
              class="day-cell"
              :class="{
                muted: !day.currentMonth,
                today: day.isToday,
                selected: day.isSelected,
                'has-events': day.events.length,
                sunday: index % 7 === 0,
                saturday: index % 7 === 6,
              }"
              :aria-pressed="day.isSelected"
              :aria-current="day.isToday ? 'date' : undefined"
              :aria-label="calendarDayLabel(day)"
              :tabindex="day.isSelected ? 0 : -1"
              @click="selectDate(day.date)"
              @keydown="handleDayKeydown"
            >
              <span>{{ day.dayNo }}</span>
              <i v-if="day.events.length" aria-hidden="true">{{ day.events.length }}</i>
            </button>
          </div>
        </section>

        <section
          v-show="activeOverviewView === 'rooms'"
          id="room-status-view-panel"
          class="status-panel"
          aria-labelledby="room-status-title"
        >
          <div class="section-heading">
            <div class="current-time status-current-time">
              <span class="time-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="8.5" />
                  <path d="M12 7.5V12l3 2" />
                </svg>
              </span>
              <div>
                <p class="date-label">{{ dateLabel }}</p>
                <time class="time-label" :datetime="now.toISOString()" aria-label="เวลาปัจจุบัน">{{ timeLabel }}</time>
              </div>
            </div>
            <div class="status-title-group">
              <div>
                <h2 id="room-status-title">สถานะห้องแบบเรียลไทม์</h2>
              </div>
            </div>
            <div class="status-heading-side">
              <button
                type="button"
                class="view-switch-button"
                aria-controls="calendar-view-panel"
                @click="activeOverviewView = 'calendar'"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3v3M17 3v3M4 8h16M5 5h14a1 1 0 0 1 1 1v14H4V6a1 1 0 0 1 1-1Z" /><path d="M8 12h3M13 12h3M8 16h3M13 16h3" /></svg>
                <span>ปฏิทิน</span>
              </button>
              <div class="status-legend" aria-label="คำอธิบายสถานะ">
                <span><i class="tone-free" />ว่าง</span>
                <span><i class="tone-busy" />ใช้งาน</span>
                <span><i class="tone-scheduled" />มีตาราง</span>
                <span><i class="tone-unknown" />ไม่มีข้อมูล</span>
              </div>
            </div>
          </div>

          <div v-if="state === 'loading'" class="panel-state" role="status">กำลังโหลดสถานะห้อง…</div>
          <div v-else-if="state === 'error' && !roomStatuses.length" class="panel-state error" role="alert">
            {{ errorMessage }}
            <button type="button" @click="loadCoreData()">ลองอีกครั้ง</button>
          </div>
          <div v-else-if="!roomStatuses.length" class="panel-state">ไม่มีข้อมูลห้องในระบบ</div>
          <div v-else class="room-floor-groups">
            <section
              v-for="group in roomStatusGroups"
              :key="group.key"
              class="room-floor-section"
              :aria-labelledby="`floor-heading-${group.key}`"
            >
              <header class="room-floor-heading">
                <h3 :id="`floor-heading-${group.key}`">
                  {{ group.floorNo === null ? "ไม่ระบุชั้น" : `ชั้น ${group.floorNo}` }}
                </h3>
                <span>{{ group.rooms.length }} ห้อง</span>
              </header>
              <div class="room-grid">
                <article
                  v-for="room in group.rooms"
                  :key="room.roomcode"
                  class="room-card"
                  :class="[`tone-${room.tone}`, { selected: sameRoom(room.roomcode, selectedRoomCode) }]"
                  role="button"
                  tabindex="0"
                  :aria-label="`ดูตารางการใช้ห้อง ${room.roomcode}`"
                  :aria-pressed="sameRoom(room.roomcode, selectedRoomCode)"
                  @click="selectRoomSchedule(room.roomcode)"
                  @keydown.enter.self="selectRoomSchedule(room.roomcode)"
                  @keydown.space.self.prevent="selectRoomSchedule(room.roomcode)"
                >
                  <div class="room-card-main">
                    <span class="room-card-head">
                      <span class="room-title">
                        <strong>{{ room.roomcode }}</strong>
                        <i class="status-dot" aria-hidden="true" />
                      </span>
                      <span class="room-card-head-actions">
                        <button
                          type="button"
                          class="room-detail-button"
                          :aria-label="roomAriaLabel(room)"
                          title="ดูรายละเอียดห้อง"
                          @click.stop="openRoomDetail(room.roomcode)"
                        >
                          <svg viewBox="0 0 24 24" aria-hidden="true">
                            <circle cx="12" cy="12" r="9" />
                            <path d="M12 10.5V16" />
                            <path d="M12 7.5h.01" />
                          </svg>
                          <span class="sr-only">ดูรายละเอียดห้อง</span>
                        </button>
                      </span>
                    </span>
                    <span class="room-status-line">
                      <span class="room-state-text">{{ room.label }}</span>
                      <span class="room-context">{{ room.context }}</span>
                    </span>
                    <span v-if="room.subjectName" class="room-subject" :title="room.subjectName" translate="no">
                      {{ room.subjectName }}
                    </span>
                    <span v-if="room.instructorName" class="room-instructor" :title="room.instructorName">
                      ผู้สอน: <span translate="no">{{ room.instructorName }}</span>
                    </span>
                    <span v-if="personCount(room.roomcode) !== undefined" class="people-count">
                      ตรวจพบ {{ personCount(room.roomcode) }} คน
                    </span>
                  </div>
                  <div v-if="room.tone === 'free'" class="room-card-actions">
                    <button
                      type="button"
                      class="room-book-button"
                      :aria-label="`จองห้อง ${room.roomcode}`"
                      @click.stop="bookRoom(room)"
                    >
                      จองห้อง
                    </button>
                  </div>
                </article>
              </div>
            </section>
          </div>
        </section>
      </div>

    </section>

    <section class="lower-grid" aria-label="ตารางการใช้ห้อง">
      <section
        class="schedule-panel"
        :class="{ 'has-room-detail': selectedRoomCode }"
        aria-labelledby="schedule-title"
      >

        <section
          v-if="selectedRoomCode"
          class="room-detail-panel schedule-room-detail desktop-room-detail"
          aria-labelledby="desktop-selected-room-title"
        >
          <header class="detail-heading">
            <div>
              <h2 id="desktop-selected-room-title">ห้อง {{ selectedRoomCode }}</h2>
              <p>{{ selectedRoom?.room_type || "ไม่มีข้อมูลประเภทห้อง" }}</p>
            </div>
            <div class="detail-heading-actions">
              <span class="status-badge" :class="`tone-${selectedRoomStatus.tone}`">
                <i aria-hidden="true" />{{ selectedRoomStatus.label }}
              </span>
              <button
                type="button"
                class="detail-close"
                aria-label="ปิดรายละเอียดห้อง"
                @click="selectedRoomCode = ''"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
              </button>
            </div>
          </header>

          <div class="room-gallery" :class="{ 'has-thumbnails': roomGalleryImages.length > 1 }">
            <div class="room-image" :class="{ empty: !roomImageUrl }">
              <button
                v-if="roomImageUrl"
                type="button"
                class="room-image-trigger"
                :aria-label="`เปิดดู${selectedGalleryImage?.alt || `ภาพห้อง ${selectedRoomCode}`}ขนาดใหญ่`"
                @click="openLightbox()"
              >
                <img
                  :src="roomImageUrl"
                  :alt="selectedGalleryImage?.alt || `ภาพห้อง ${selectedRoomCode}`"
                  @error="imageFailed = true"
                >
                <span class="image-zoom-hint" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 4 4M10.5 7.5v6M7.5 10.5h6" /></svg>
                  ดูภาพใหญ่
                </span>
              </button>
              <div v-else class="image-empty">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM4 15l4-4 4 4 3-3 5 5M9 9h.01" /></svg>
                <span>ไม่มีรูปห้องในข้อมูลปัจจุบัน</span>
              </div>
            </div>

            <div v-if="roomGalleryImages.length > 1" class="room-thumbnails" aria-label="รูปภาพห้องทั้งหมด">
              <button
                v-for="(image, index) in visibleGalleryThumbnails"
                :key="image.src"
                type="button"
                class="room-thumbnail"
                :class="{ selected: selectedGalleryIndex === index }"
                :aria-pressed="selectedGalleryIndex === index"
                :aria-label="`แสดง${image.alt}`"
                @click="selectRoomImage(index)"
              >
                <img :src="image.src" :alt="image.alt" loading="lazy">
              </button>
              <button
                v-if="remainingGalleryCount"
                type="button"
                class="room-thumbnail room-thumbnail-more"
                :class="{ selected: selectedGalleryIndex >= 4 }"
                :aria-pressed="selectedGalleryIndex >= 4"
                :aria-label="`ดูรูปที่เหลืออีก ${remainingGalleryCount} รูป`"
                @click="openLightbox(4)"
              >
                <span>+{{ remainingGalleryCount }}</span>
                <small>รูป</small>
              </button>
            </div>
          </div>

          <dl class="room-facts">
            <div><dt>อาคาร</dt><dd translate="no">{{ selectedRoom?.building || "ไม่มีข้อมูล" }}</dd></div>
            <div><dt>ชั้น</dt><dd>{{ selectedRoomStatus.floorNo ?? selectedRoom?.floor_no ?? "ไม่มีข้อมูล" }}</dd></div>
            <div><dt>คอมพิวเตอร์</dt><dd>{{ selectedRoom?.computer_no ?? "ไม่มีข้อมูล" }}</dd></div>
            <div><dt>ที่นั่ง</dt><dd>{{ selectedRoom?.seat_no ?? "ไม่มีข้อมูล" }}</dd></div>
          </dl>

          <div class="feature-section">
            <div class="feature-heading"><h3>โปรแกรมที่ติดตั้ง</h3><span>{{ activeApplications.length }}</span></div>
            <p v-if="detailLoading" class="feature-empty" role="status">กำลังโหลด…</p>
            <div v-else-if="activeApplications.length" class="chip-row">
              <span v-for="item in activeApplications" :key="featureLabel(item)" class="feature-chip" translate="no">
                <img
                  v-if="featureIconUrl(item, 'application')"
                  :src="featureIconUrl(item, 'application')"
                  alt=""
                  loading="lazy"
                  @error="hideBrokenFeatureIcon"
                >
                <span>{{ featureLabel(item) }}</span>
              </span>
            </div>
            <p v-else class="feature-empty">ไม่มีข้อมูลโปรแกรม</p>
          </div>

          <div class="feature-section">
            <div class="feature-heading"><h3>อุปกรณ์</h3><span>{{ activeAccessories.length }}</span></div>
            <div v-if="activeAccessories.length" class="chip-row">
              <span v-for="item in activeAccessories" :key="featureLabel(item)" class="feature-chip" translate="no">
                <img
                  v-if="featureIconUrl(item, 'accessory')"
                  :src="featureIconUrl(item, 'accessory')"
                  alt=""
                  loading="lazy"
                  @error="hideBrokenFeatureIcon"
                >
                <span>{{ featureLabel(item) }}</span>
              </span>
            </div>
            <p v-else class="feature-empty">ไม่มีข้อมูลอุปกรณ์</p>
          </div>
          <p v-if="detailError" class="detail-error" role="alert">{{ detailError }}</p>
        </section>

        <header class="schedule-heading">
          <div class="schedule-title-group">
            <span class="schedule-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M7 3v3M17 3v3M4 8h16M5 5h14a1 1 0 0 1 1 1v14H4V6a1 1 0 0 1 1-1Z" /><path d="M8 12h3M13 12h3M8 16h3M13 16h3" /></svg>
            </span>
            <div><h2 id="schedule-title">ตารางการใช้ห้อง</h2><p>{{ scheduleDateLabel }}</p></div>
          </div>
          <span>{{ selectedDaySchedules.length }} รายการ</span>
        </header>
        <div v-if="!selectedDaySchedules.length" class="schedule-empty">
          {{ activeOverviewView === "rooms" ? "ไม่มีรายการในวันปัจจุบัน" : "ไม่มีรายการในวันที่เลือก" }}
        </div>
        <div v-else class="schedule-table" role="table" :aria-label="`ตารางการใช้ห้อง ${scheduleDateLabel}`">
          <div class="schedule-table-head" role="row">
            <span role="columnheader">เวลา</span>
            <span role="columnheader">ห้อง</span>
            <span role="columnheader">รายวิชา / ผู้สอน</span>
            <span role="columnheader">สถานะ</span>
          </div>
          <div class="schedule-table-body" role="rowgroup">
            <div
              v-for="item in selectedDaySchedules"
              :key="`${item.schedule_id ?? item.id ?? item.rowId}-${item.roomcode}-${item.startTime}`"
              class="schedule-table-row"
              :class="{ past: isSchedulePast(item) }"
              :aria-label="isSchedulePast(item) ? 'รายการที่ผ่านมาแล้ว' : undefined"
              role="row"
            >
              <time role="cell">{{ item.startTime }}–{{ item.finishTime }}</time>
              <strong class="schedule-room" role="cell">{{ item.roomcode || selectedRoomCode }}</strong>
              <div class="schedule-subject" role="cell">
                <span class="schedule-course" translate="no">{{ scheduleTitle(item) }}</span>
                <span class="schedule-owner">ผู้สอน: <span translate="no">{{ scheduleOwner(item) || "—" }}</span></span>
              </div>
              <span class="schedule-state" :class="scheduleTone(item)" role="cell">{{ scheduleStatus(item) }}</span>
            </div>
          </div>
        </div>
      </section>
    </section>
    <dialog
      ref="roomDetailDialog"
      class="room-detail-dialog"
      aria-labelledby="selected-room-title"
      @click.self="closeRoomDetail"
      @close="handleRoomDetailClosed"
    >
      <section class="room-detail-panel modal-room-detail">
          <header class="detail-heading">
            <div>
              <h2 id="selected-room-title">ห้อง {{ selectedRoomCode }}</h2>
              <p>{{ selectedRoom?.room_type || "ไม่มีข้อมูลประเภทห้อง" }}</p>
            </div>
            <div class="detail-heading-actions">
              <span class="status-badge" :class="`tone-${selectedRoomStatus.tone}`">
                <i aria-hidden="true" />{{ selectedRoomStatus.label }}
              </span>
              <button type="button" class="detail-close" aria-label="ปิดรายละเอียดห้อง" autofocus @click="closeRoomDetail">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
              </button>
            </div>
          </header>

          <div class="room-gallery" :class="{ 'has-thumbnails': roomGalleryImages.length > 1 }">
            <div class="room-image" :class="{ empty: !roomImageUrl }">
              <button
                v-if="roomImageUrl"
                type="button"
                class="room-image-trigger"
                :aria-label="`เปิดดู${selectedGalleryImage?.alt || `ภาพห้อง ${selectedRoomCode}`}ขนาดใหญ่`"
                @click="openLightbox()"
              >
                <img
                  :src="roomImageUrl"
                  :alt="selectedGalleryImage?.alt || `ภาพห้อง ${selectedRoomCode}`"
                  @error="imageFailed = true"
                >
                <span class="image-zoom-hint" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 4 4M10.5 7.5v6M7.5 10.5h6" /></svg>
                  ดูภาพใหญ่
                </span>
              </button>
              <div v-else class="image-empty">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM4 15l4-4 4 4 3-3 5 5M9 9h.01" /></svg>
                <span>ไม่มีรูปห้องในข้อมูลปัจจุบัน</span>
              </div>
            </div>

            <div v-if="roomGalleryImages.length > 1" class="room-thumbnails" aria-label="รูปภาพห้องทั้งหมด">
              <button
                v-for="(image, index) in visibleGalleryThumbnails"
                :key="image.src"
                type="button"
                class="room-thumbnail"
                :class="{ selected: selectedGalleryIndex === index }"
                :aria-pressed="selectedGalleryIndex === index"
                :aria-label="`แสดง${image.alt}`"
                @click="selectRoomImage(index)"
              >
                <img :src="image.src" :alt="image.alt" loading="lazy">
              </button>
              <button
                v-if="remainingGalleryCount"
                type="button"
                class="room-thumbnail room-thumbnail-more"
                :class="{ selected: selectedGalleryIndex >= 4 }"
                :aria-pressed="selectedGalleryIndex >= 4"
                :aria-label="`ดูรูปที่เหลืออีก ${remainingGalleryCount} รูป`"
                @click="openLightbox(4)"
              >
                <span>+{{ remainingGalleryCount }}</span>
                <small>รูป</small>
              </button>
            </div>
          </div>

          <dl class="room-facts">
            <div><dt>อาคาร</dt><dd translate="no">{{ selectedRoom?.building || "ไม่มีข้อมูล" }}</dd></div>
            <div><dt>ชั้น</dt><dd>{{ selectedRoomStatus.floorNo ?? selectedRoom?.floor_no ?? "ไม่มีข้อมูล" }}</dd></div>
            <div><dt>คอมพิวเตอร์</dt><dd>{{ selectedRoom?.computer_no ?? "ไม่มีข้อมูล" }}</dd></div>
            <div><dt>ที่นั่ง</dt><dd>{{ selectedRoom?.seat_no ?? "ไม่มีข้อมูล" }}</dd></div>
          </dl>

          <div class="feature-section">
            <div class="feature-heading"><h3>โปรแกรมที่ติดตั้ง</h3><span>{{ activeApplications.length }}</span></div>
            <p v-if="detailLoading" class="feature-empty" role="status">กำลังโหลด…</p>
            <div v-else-if="activeApplications.length" class="chip-row">
              <span v-for="item in activeApplications" :key="featureLabel(item)" class="feature-chip" translate="no">
                <img
                  v-if="featureIconUrl(item, 'application')"
                  :src="featureIconUrl(item, 'application')"
                  alt=""
                  loading="lazy"
                  @error="hideBrokenFeatureIcon"
                >
                <span>{{ featureLabel(item) }}</span>
              </span>
            </div>
            <p v-else class="feature-empty">ไม่มีข้อมูลโปรแกรม</p>
          </div>

          <div class="feature-section">
            <div class="feature-heading"><h3>อุปกรณ์</h3><span>{{ activeAccessories.length }}</span></div>
            <div v-if="activeAccessories.length" class="chip-row">
              <span v-for="item in activeAccessories" :key="featureLabel(item)" class="feature-chip" translate="no">
                <img
                  v-if="featureIconUrl(item, 'accessory')"
                  :src="featureIconUrl(item, 'accessory')"
                  alt=""
                  loading="lazy"
                  @error="hideBrokenFeatureIcon"
                >
                <span>{{ featureLabel(item) }}</span>
              </span>
            </div>
            <p v-else class="feature-empty">ไม่มีข้อมูลอุปกรณ์</p>
          </div>
          <p v-if="detailError" class="detail-error" role="alert">{{ detailError }}</p>
      </section>
    </dialog>
  </div>

  <Teleport to="body">
    <div v-if="showApiModal" class="api-error-overlay" role="presentation">
      <section
        class="api-error-modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="api-error-title"
        aria-describedby="api-error-detail"
      >
        <span class="api-error-modal-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M12 3 1.8 20.4h20.4z" />
            <path d="M12 9.4v4.4" />
            <path d="M12 16.9h.01" />
          </svg>
        </span>
        <h2 id="api-error-title">ไม่สามารถเชื่อมต่อกับระบบได้</h2>
        <p id="api-error-detail">{{ apiStatusDetail }}</p>
        <p class="api-error-modal-note">
          ข้อมูลที่แสดงอยู่อาจไม่เป็นปัจจุบัน ระบบจะพยายามเชื่อมต่อใหม่ทุก 30 วินาที
          <template v-if="apiCheckedAtLabel"><br />ตรวจสอบล่าสุด {{ apiCheckedAtLabel }} น.</template>
        </p>
        <p class="api-error-modal-contact">
          <span class="api-error-contact-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path d="M4.5 5.5h4l1.6 4-2.3 1.6a11.5 11.5 0 0 0 4.9 4.9l1.6-2.3 4 1.6v4a1.5 1.5 0 0 1-1.6 1.5A15.5 15.5 0 0 1 3 7.1 1.5 1.5 0 0 1 4.5 5.5z" />
            </svg>
          </span>
          ติดต่อผู้ดูแลระบบ
        </p>
        <div class="api-error-modal-actions">
          <button type="button" class="api-error-modal-dismiss" @click="dismissApiModal">ปิดหน้าต่าง</button>
          <button
            type="button"
            class="api-error-modal-retry"
            autofocus
            :disabled="apiRetrying"
            @click="retryConnection"
          >
            {{ apiRetrying ? "กำลังเชื่อมต่อ…" : "ลองเชื่อมต่อใหม่" }}
          </button>
        </div>
      </section>
    </div>
  </Teleport>

  <Teleport to="body">
    <div
      v-if="loginPromptVisible"
      class="login-prompt-overlay"
      role="presentation"
      @click.self="closeLoginPrompt"
      @keydown.esc="closeLoginPrompt"
    >
      <section class="login-prompt" role="dialog" aria-modal="true" aria-labelledby="login-prompt-title">
        <button type="button" class="login-prompt-close" aria-label="ปิดข้อความ" @click="closeLoginPrompt">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
        <span class="login-prompt-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15m3 0 3-3m0 0-3-3m3 3H9" /></svg>
        </span>
        <h2 id="login-prompt-title">กรุณาเข้าสู่ระบบ</h2>
        <p>คุณต้องเข้าสู่ระบบก่อนจึงจะสามารถจองห้องได้</p>
        <div class="login-prompt-actions">
          <button type="button" class="login-prompt-cancel" @click="closeLoginPrompt">ไว้ก่อน</button>
          <button type="button" class="login-prompt-primary" autofocus @click="goToLogin">
            ไปหน้าเข้าสู่ระบบ
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
          </button>
        </div>
      </section>
    </div>
  </Teleport>

  <Teleport to="body">
    <dialog
      ref="lightboxDialog"
      class="room-lightbox"
      aria-labelledby="room-lightbox-title"
      @click.self="closeLightbox"
      @keydown="handleLightboxKeydown"
    >
      <div class="lightbox-shell">
        <header class="lightbox-header">
          <div>
            <h2 id="room-lightbox-title">ห้อง {{ selectedRoomCode }}</h2>
            <p v-if="roomGalleryImages.length > 1">รูปที่ {{ selectedGalleryIndex + 1 }} จาก {{ roomGalleryImages.length }}</p>
          </div>
          <button type="button" class="lightbox-close" aria-label="ปิดหน้าต่างดูรูป" autofocus @click="closeLightbox">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </header>

        <div class="lightbox-stage">
          <button
            v-if="roomGalleryImages.length > 1"
            type="button"
            class="lightbox-nav previous"
            aria-label="ดูรูปก่อนหน้า"
            @click="stepLightbox(-1)"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
          </button>

          <img
            v-if="selectedGalleryImage && !lightboxImageFailed"
            :src="selectedGalleryImage.src"
            :alt="selectedGalleryImage.alt"
            @error="lightboxImageFailed = true"
          />
          <div v-else class="lightbox-error" role="status">ไม่สามารถแสดงรูปนี้ได้</div>

          <button
            v-if="roomGalleryImages.length > 1"
            type="button"
            class="lightbox-nav next"
            aria-label="ดูรูปถัดไป"
            @click="stepLightbox(1)"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
          </button>
        </div>

        <div v-if="roomGalleryImages.length > 1" class="lightbox-thumbnails" aria-label="เลือกรูปที่ต้องการดู">
          <button
            v-for="(image, index) in roomGalleryImages"
            :key="`lightbox-${image.src}`"
            type="button"
            :class="{ selected: selectedGalleryIndex === index }"
            :aria-pressed="selectedGalleryIndex === index"
            :aria-label="`ดู${image.alt}`"
            @click="selectRoomImage(index); lightboxImageFailed = false"
          >
            <img :src="image.src" alt="" />
          </button>
        </div>
      </div>
    </dialog>
  </Teleport>
</template>

<style scoped>
.dashboard-test-page {
  --dt-page: var(--bg-page, #f1f5f9);
  --dt-surface: var(--bg-surface, #fff);
  --dt-surface-alt: var(--input-bg, #f8fafc);
  --dt-border: var(--border, #d7dee8);
  --dt-text: var(--text-primary, #0f172a);
  --dt-muted: var(--text-secondary, #526174);
  --dt-soft: var(--text-secondary, #475569);
  --dt-blue: var(--dashboard-accent);
  --dt-blue-soft: var(--dashboard-accent-soft);
  --dt-green: var(--status-free-text);
  --dt-green-soft: var(--status-free-soft);
  --dt-red: var(--status-busy-text);
  --dt-red-soft: var(--status-busy-soft);
  --dt-amber: var(--status-pending-text);
  --dt-amber-soft: var(--status-pending-soft);
  --dt-gray: var(--status-unknown-text);
  --dt-info-secondary: var(--text-secondary);
  --dt-room-card: var(--dashboard-surface-muted);
  --dt-room-shadow: var(--dashboard-shadow);
  --dt-room-shadow-hover: var(--dashboard-tooltip-shadow);
  --dt-panel-shadow: var(--dashboard-shadow);
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(19rem, calc(33% + 1.6rem));
  grid-template-rows: 5rem calc(100dvh - 9.6rem);
  column-gap: 0.65rem;
  row-gap: 0.46rem;
  width: 100%;
  min-height: calc(100dvh - 3.1rem);
  min-width: 0;
  padding: 0.52rem 35px;
  box-sizing: border-box;
  overflow: visible;
  background: var(--dt-page);
  color: var(--dt-text);
  font-size: 14px;
}

.overview-grid,
.overview-main,
.lower-grid { display: contents; }
.mobile-current-time { display: none; }
.kpi-grid { grid-column: 1; grid-row: 1; }
.calendar-panel-expanded { grid-column: 1; grid-row: 2; }
.status-panel { grid-column: 1; grid-row: 2; }
.schedule-panel { grid-column: 2; grid-row: 1 / 3; }

.time-strip,
.kpi-card,
.status-panel,
.room-detail-panel,
.calendar-panel,
.schedule-panel {
  border: 1px solid color-mix(in srgb, var(--dt-border) 78%, transparent);
  border-radius: 12px;
  background: var(--dt-surface);
  box-sizing: border-box;
}

.kpi-card,
.status-panel,
.room-detail-panel,
.calendar-panel,
.schedule-panel { box-shadow: var(--dt-panel-shadow); }

.time-strip {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  min-height: 3.25rem;
  padding: 0.34rem 0.58rem;
  border-color: color-mix(in srgb, var(--dt-blue) 22%, var(--dt-border));
  background: color-mix(in srgb, var(--dt-blue-soft) 58%, var(--dt-surface));
}

.current-time {
  grid-column: 2;
  display: flex;
  align-items: center;
  gap: 0.58rem;
  min-width: 0;
}

.time-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2.35rem;
  height: 2.35rem;
  flex: 0 0 auto;
  border-radius: 10px;
  background: var(--dt-blue);
  color: #fff;
}

.time-icon svg {
  width: 1.42rem;
  height: 1.42rem;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.date-label,
.time-label,
.kpi-card p,
.kpi-card span,
.section-heading p,
.detail-heading p,
.calendar-heading p,
.schedule-heading p { margin: 0; }

.date-label { color: var(--dt-muted); font-size: 0.73rem; font-weight: 650; }
.time-label { display: block; margin-top: 0.04rem; color: var(--dt-text); font-size: 1.82rem; line-height: 0.92; font-weight: 850; font-variant-numeric: tabular-nums; letter-spacing: -0.025em; }
.live-indicator { grid-column: 3; display: inline-flex; align-items: center; justify-self: end; gap: 0.38rem; color: var(--dt-muted); font-size: 0.72rem; font-weight: 650; }
.live-dot { width: 0.52rem; height: 0.52rem; border-radius: 50%; background: var(--dt-gray); }
.live-dot.connected { background: var(--dt-green); }

.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.38rem; min-width: 0; }
.kpi-card { position: relative; display: grid; grid-template-columns: 2.55rem minmax(0, 1fr); align-items: center; gap: 0.62rem; min-width: 0; min-height: 5rem; padding: 0.52rem 0.68rem; overflow: hidden; }
.kpi-card > div { display: flex; min-width: 0; flex-direction: column; justify-content: center; }
.kpi-icon { display: inline-flex; align-items: center; justify-content: center; width: 2.45rem; height: 2.45rem; border-radius: 10px; background: var(--kpi-soft); }
.kpi-icon svg { width: 1.5rem; height: 1.5rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.kpi-card p { overflow: hidden; color: var(--dt-soft); font-size: calc(0.72rem + 2px); font-weight: 700; line-height: 1.15; white-space: nowrap; text-overflow: ellipsis; }
.kpi-card strong { display: flex; align-items: baseline; gap: 0.28rem; margin: 0.08rem 0 0.06rem; font-size: 1.55rem; line-height: 0.95; font-variant-numeric: tabular-nums; }
.kpi-card strong small { color: var(--dt-muted); font-size: calc(0.68rem + 2px); font-weight: 650; line-height: 1; }
.kpi-card div > span { display: block; overflow: hidden; color: var(--dt-soft); font-size: calc(0.66rem + 2px); line-height: 1.15; white-space: nowrap; text-overflow: ellipsis; }
.kpi-blue { --kpi-soft: var(--dt-blue-soft); }
.kpi-green { --kpi-soft: var(--dt-green-soft); }
.kpi-red { --kpi-soft: var(--dt-red-soft); }
.kpi-amber { --kpi-soft: var(--dt-amber-soft); }
.kpi-blue .kpi-icon { color: var(--dt-blue); }
.kpi-green .kpi-icon { color: var(--dt-green); }
.kpi-red .kpi-icon { color: var(--dt-red); }
.kpi-amber .kpi-icon { color: var(--dt-amber); }

.status-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  padding: 0.65rem 0.72rem 0.72rem;
  border-radius: 16px;
  background: var(--dt-surface);
}
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 0.7rem; margin-bottom: 0.55rem; }
.status-panel > .section-heading { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 0.9rem; }
.status-current-time { grid-column: 1; align-self: start; justify-self: start; }
.status-title-group { grid-column: 2; justify-self: center; text-align: center; }
.status-heading-side { grid-column: 3; justify-self: end; }
.status-heading-side { display: flex; flex-direction: column; align-items: flex-end; gap: 0.38rem; min-width: 0; }
.section-heading h2,
.detail-heading h2,
.calendar-heading h2,
.schedule-heading h2 { margin: 0; color: var(--dt-text); font-size: 0.94rem; line-height: 1.25; font-weight: 800; }
.section-heading p,
.detail-heading p,
.calendar-heading p,
.schedule-heading p { margin-top: 0.08rem; color: var(--dt-soft); font-size: calc(0.7rem + 2px); }
.status-legend { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0.25rem 0.65rem; color: var(--dt-muted); font-size: calc(0.68rem + 2px); }
.status-legend span { display: inline-flex; align-items: center; gap: 0.25rem; white-space: nowrap; }
.status-legend i,
.status-badge i { width: 0.45rem; height: 0.45rem; border-radius: 50%; background: currentColor; }
.tone-free { color: var(--dt-green); }
.tone-busy { color: var(--dt-red); }
.tone-scheduled { color: var(--dt-amber); }
.tone-unknown { color: var(--dt-gray); }

.room-floor-groups { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; }
.room-floor-section + .room-floor-section { margin-top: 0.72rem; }
.room-floor-heading { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; margin-bottom: 0.38rem; padding: 0.2rem 0.08rem 0.34rem; border-bottom: 1px solid color-mix(in srgb, var(--dt-blue) 18%, var(--dt-border)); background: var(--dt-surface); }
.room-floor-heading h3 { margin: 0; color: var(--dt-text); font-size: calc(0.8rem + 2px); line-height: 1.2; font-weight: 850; }
.room-floor-heading > span { padding: 0.16rem 0.38rem; border-radius: 999px; background: var(--dt-blue-soft); color: var(--dt-blue); font-size: calc(0.64rem + 2px); font-weight: 750; white-space: nowrap; }
.room-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); column-gap: 0.5rem; row-gap: 0.56rem; min-width: 0; min-height: 0; padding: 0.16rem 0.16rem 0.34rem; }
.room-card { position: relative; display: flex; flex-direction: column; align-items: stretch; min-width: 0; min-height: calc(5.7rem - 3px); padding: calc(0.58rem - 1.5px) 0.64rem; overflow: hidden; border: 1px solid var(--room-tone-border, var(--dt-border)); border-left-width: 4px; border-left-color: var(--room-tone-accent, var(--dt-gray)); border-radius: 10px; background: var(--room-tone-bg, var(--dt-room-card)); color: var(--dt-text); box-shadow: var(--dt-room-shadow); cursor: pointer; transition: border-color 140ms ease-out, background-color 140ms ease-out, box-shadow 140ms ease-out, transform 140ms ease-out; }
.room-card:hover { background: color-mix(in srgb, var(--room-tone-bg, var(--dt-room-card)) 82%, var(--dt-blue-soft)); box-shadow: var(--dt-room-shadow-hover); transform: translateY(-1px); }
.room-card:focus-visible { outline: 2px solid var(--dt-blue); outline-offset: 2px; }
.room-card-main { display: flex; flex: 1; min-width: 0; flex-direction: column; align-items: stretch; color: inherit; text-align: left; }
.room-detail-button:focus-visible,
.room-book-button:focus-visible,
.calendar-controls button:focus-visible,
.day-cell:focus-visible,
.panel-state button:focus-visible { outline: 2px solid var(--dt-blue); outline-offset: 2px; }
.room-card.tone-free { --room-tone-bg: color-mix(in srgb, var(--dt-green-soft) 62%, var(--dt-room-card)); --room-tone-border: color-mix(in srgb, var(--dt-green) 24%, var(--dt-border)); --room-tone-accent: color-mix(in srgb, var(--dt-green) 68%, var(--dt-border)); }
.room-card.tone-busy { --room-tone-bg: color-mix(in srgb, var(--dt-red-soft) 58%, var(--dt-room-card)); --room-tone-border: color-mix(in srgb, var(--dt-red) 24%, var(--dt-border)); --room-tone-accent: color-mix(in srgb, var(--dt-red) 68%, var(--dt-border)); }
.room-card.tone-scheduled { --room-tone-bg: color-mix(in srgb, var(--dt-amber-soft) 65%, var(--dt-room-card)); --room-tone-border: color-mix(in srgb, var(--dt-amber) 24%, var(--dt-border)); --room-tone-accent: color-mix(in srgb, var(--dt-amber) 68%, var(--dt-border)); }
.room-card.tone-unknown { --room-tone-bg: var(--dt-room-card); --room-tone-border: var(--dt-border); --room-tone-accent: color-mix(in srgb, var(--dt-gray) 68%, var(--dt-border)); }
.room-card.selected { border-color: rgb(255 255 255 / 0.52); border-left-color: rgb(255 255 255 / 0.82); background: var(--dt-blue); color: #fff; box-shadow: 0 7px 16px rgb(37 99 235 / 0.28); }
.room-card.tone-free .status-dot,
.room-card.tone-free .room-state-text { color: var(--dt-green); }
.room-card.tone-busy .status-dot,
.room-card.tone-busy .room-state-text { color: var(--dt-red); }
.room-card.tone-scheduled .status-dot,
.room-card.tone-scheduled .room-state-text { color: var(--dt-amber); }
.room-card.tone-unknown .status-dot,
.room-card.tone-unknown .room-state-text { color: var(--dt-gray); }
.room-card.selected .status-dot,
.room-card.selected .room-state-text,
.room-card.selected .room-context,
.room-card.selected .room-subject,
.room-card.selected .room-instructor,
.room-card.selected .people-count { color: #fff; }
.room-card-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 0.42rem; }
.room-card-head-actions { display: inline-flex; align-items: center; gap: 0.34rem; }
.room-title { display: flex; min-width: 0; align-items: center; gap: 0.32rem; }
.room-card-head strong { min-width: 0; overflow: hidden; font-size: 0.94rem; text-overflow: ellipsis; white-space: nowrap; }
.status-dot { width: 0.52rem; height: 0.52rem; flex: 0 0 auto; border-radius: 50%; background: currentColor; }
.room-status-line { display: flex; min-width: 0; align-items: baseline; gap: 0.3rem; margin-top: 0.09rem; overflow: hidden; }
.room-state-text { min-width: 0; overflow: hidden; font-size: calc(0.72rem + 2px); font-weight: 750; text-overflow: ellipsis; white-space: nowrap; }
.room-context,
.room-subject,
.room-instructor,
.people-count { margin-top: 0.04rem; overflow: hidden; color: var(--dt-muted); font-size: calc(0.66rem + 2px); text-overflow: ellipsis; white-space: nowrap; }
.room-status-line .room-context { flex: 0 0 auto; margin-top: 0; margin-left: auto; }
.room-subject { color: var(--dt-text); font-weight: 650; }
.room-instructor { color: var(--dt-soft); font-weight: 400; }
.people-count { color: var(--dt-blue); font-weight: 650; }
.room-card-actions { display: flex; align-items: stretch; justify-content: flex-end; gap: 0.3rem; margin-top: 0.34rem; }
.room-detail-button,
.room-book-button { min-height: 2rem; padding: 0.24rem 0.5rem; border-radius: 6px; font: inherit; font-size: calc(0.66rem + 2px); font-weight: 750; line-height: 1.2; cursor: pointer; transition: background-color 140ms ease-out, border-color 140ms ease-out, box-shadow 140ms ease-out, transform 140ms ease-out; }
.room-detail-button { display: none; width: 1.7rem; min-width: 1.7rem; min-height: 1.7rem; flex: 0 0 auto; padding: 0; border: 0; background: transparent; color: var(--dt-info-secondary); box-shadow: none; }
.room-detail-button svg { width: 1.15rem; height: 1.15rem; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.room-detail-button:hover { background: var(--dt-blue-soft); transform: translateY(-1px); }
.room-detail-button:active { background: color-mix(in srgb, var(--dt-blue) 16%, transparent); transform: none; }
.room-book-button { flex: 0 0 auto; border: 1px solid var(--brand-primary); background: var(--brand-primary); color: var(--brand-on-primary); box-shadow: 0 2px 5px color-mix(in srgb, var(--brand-primary) 24%, transparent); }
.room-book-button:hover { border-color: color-mix(in srgb, var(--brand-primary) 82%, #000); background: color-mix(in srgb, var(--brand-primary) 82%, #000); box-shadow: 0 3px 7px color-mix(in srgb, var(--brand-primary) 30%, transparent); transform: translateY(-1px); }
.room-card.selected .room-detail-button { background: transparent; color: #fff; }
.room-card.selected .room-detail-button:hover { background: rgb(255 255 255 / 0.16); }
.room-card.selected .room-book-button { border-color: rgb(255 255 255 / 0.5); background: rgb(255 255 255 / 0.16); color: #fff; }

.panel-state,
.detail-empty,
.schedule-empty { display: flex; align-items: center; justify-content: center; flex: 1; min-height: 5rem; color: var(--dt-muted); font-size: 0.78rem; text-align: center; }
.panel-state.error { flex-direction: column; gap: 0.5rem; color: var(--dt-red); }
.panel-state button { border: 1px solid var(--dt-border); border-radius: 6px; padding: 0.35rem 0.6rem; background: var(--dt-surface); color: var(--dt-text); font: inherit; cursor: pointer; }

/* Fixed rather than a grid child: .dashboard-test-page pins its rows to the
   viewport height, so a banner in the flow would squeeze the panels and the
   layout would shift every time the API blinked. */
.api-offline-banner { position: fixed; z-index: 650; top: 0.5rem; left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 0.7rem; width: min(calc(100vw - 1rem), 46rem); box-sizing: border-box; padding: 0.6rem 0.8rem; border: 1px solid var(--dt-red); border-radius: 11px; background: var(--dt-red-soft); color: var(--dt-red); box-shadow: 0 10px 30px rgb(15 23 42 / 0.2); }
.api-offline-icon { display: grid; place-items: center; flex: none; width: 1.9rem; height: 1.9rem; border-radius: 8px; background: var(--dt-surface); }
.api-offline-icon svg { width: 1.15rem; height: 1.15rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.api-offline-text { min-width: 0; flex: 1 1 auto; }
.api-offline-title { margin: 0; font-size: 0.88rem; font-weight: 750; }
.api-offline-detail { margin: 0.1rem 0 0; color: var(--dt-text); font-size: 0.75rem; line-height: 1.45; opacity: 0.85; }
.api-offline-retry { flex: none; min-height: 2.1rem; padding: 0 0.75rem; border: 1px solid var(--dt-red); border-radius: 8px; background: var(--dt-surface); color: var(--dt-red); font: inherit; font-size: 0.78rem; font-weight: 700; cursor: pointer; }
.api-offline-retry:hover:not(:disabled) { background: var(--dt-red); color: var(--dt-surface); }
.api-offline-retry:focus-visible { outline: 2px solid var(--dt-red); outline-offset: 2px; }
.api-offline-retry:disabled { opacity: 0.6; cursor: progress; }

/* z-index above .login-prompt-overlay (700): if the API is unreachable the
   booking prompt behind it cannot lead anywhere, so this has to be on top. */
.api-error-overlay { position: fixed; inset: 0; z-index: 800; display: grid; place-items: center; padding: 1rem; background: rgb(15 23 42 / 0.72); backdrop-filter: blur(3px); }
.api-error-modal { width: min(100%, 27rem); box-sizing: border-box; padding: 1.8rem 1.6rem 1.5rem; border-radius: 14px; background: var(--bg-surface, #fff); color: var(--text-primary, #0f172a); box-shadow: 0 24px 70px rgb(15 23 42 / 0.36); text-align: center; }
.api-error-modal-icon { display: grid; place-items: center; width: 3.1rem; height: 3.1rem; margin: 0 auto 0.9rem; border-radius: 12px; background: var(--status-busy-soft, #fee2e2); color: var(--status-busy-text, #b91c1c); }
.api-error-modal-icon svg { width: 1.6rem; height: 1.6rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.api-error-modal h2 { margin: 0; font-size: 1.18rem; letter-spacing: -0.015em; }
.api-error-modal p { margin: 0.45rem auto 0; max-width: 34ch; color: var(--text-secondary, #526174); font-size: 0.86rem; line-height: 1.55; }
.api-error-modal-note { font-size: 0.78rem !important; opacity: 0.85; }
.api-error-modal-contact { display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem; margin-top: 0.9rem !important; padding: 0.4rem 0.75rem; border: 1px dashed var(--border, #d7dee8); border-radius: 8px; background: var(--bg-surface-alt, #f8fafc); color: var(--text-primary, #0f172a) !important; font-size: 0.82rem !important; font-weight: 700; }
.api-error-contact-icon { display: inline-grid; place-items: center; flex: none; color: var(--status-busy-text, #b91c1c); }
.api-error-contact-icon svg { width: 0.95rem; height: 0.95rem; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.api-error-modal-actions { display: grid; grid-template-columns: 1fr 1.3fr; gap: 0.6rem; margin-top: 1.4rem; }
.api-error-modal-actions button { min-height: 2.7rem; border-radius: 9px; font: inherit; font-size: 0.83rem; font-weight: 750; cursor: pointer; }
.api-error-modal-dismiss { border: 1px solid var(--border, #d7dee8); background: var(--bg-surface, #fff); color: var(--text-secondary, #526174); }
.api-error-modal-dismiss:hover { background: var(--bg-surface-alt, #f8fafc); color: var(--text-primary, #0f172a); }
.api-error-modal-retry { border: 1px solid var(--status-busy-text, #b91c1c); background: var(--status-busy-text, #b91c1c); color: #fff; }
.api-error-modal-retry:hover:not(:disabled) { background: color-mix(in srgb, var(--status-busy-text, #b91c1c) 84%, #000); }
.api-error-modal-retry:disabled { opacity: 0.7; cursor: progress; }
.api-error-modal-actions button:focus-visible { outline: 2px solid var(--status-busy-text, #b91c1c); outline-offset: 2px; }

@media (max-width: 30rem) {
  .api-error-modal-actions { grid-template-columns: 1fr; }
}

@media (max-width: 36rem) {
  .api-offline-banner { flex-wrap: wrap; row-gap: 0.5rem; }
  .api-offline-retry { width: 100%; }
}

.room-detail-panel { min-width: 0; min-height: 0; padding: 0.62rem; overflow-y: auto; background: color-mix(in srgb, var(--dt-blue-soft) 20%, var(--dt-surface)); scrollbar-width: thin; }
.room-detail-dialog { width: min(92vw, 42rem); max-width: none; max-height: min(90dvh, 48rem); padding: 0; overflow: hidden; border: 1px solid var(--dt-border); border-radius: 14px; background: var(--dt-surface); color: var(--dt-text); box-shadow: 0 24px 70px rgb(15 23 42 / 0.34); }
.room-detail-dialog::backdrop { background: rgb(15 23 42 / 0.68); backdrop-filter: blur(3px); }
.login-prompt-overlay { position: fixed; inset: 0; z-index: 700; display: grid; place-items: center; padding: 1rem; background: rgb(15 23 42 / 0.68); backdrop-filter: blur(3px); }
.login-prompt { position: relative; width: min(100%, 25rem); box-sizing: border-box; padding: 1.7rem; border-radius: 14px; background: var(--bg-surface); color: var(--text-primary); box-shadow: 0 24px 70px rgb(15 23 42 / 0.34); text-align: center; }
.login-prompt-icon { display: grid; place-items: center; width: 3rem; height: 3rem; margin: 0 auto 0.85rem; border-radius: 12px; background: var(--dashboard-accent-soft); color: var(--accent-link); }
.login-prompt-icon svg { width: 1.45rem; height: 1.45rem; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.login-prompt h2 { margin: 0; font-size: 1.15rem; letter-spacing: -0.015em; }
.login-prompt p { margin: 0.4rem auto 0; max-width: 32ch; color: var(--text-secondary); font-size: 0.84rem; line-height: 1.55; }
.login-prompt-close { position: absolute; top: 0.75rem; right: 0.75rem; display: grid; place-items: center; width: 2rem; height: 2rem; padding: 0; border: 1px solid var(--border); border-radius: 50%; background: transparent; color: var(--text-secondary); cursor: pointer; }
.login-prompt-close:hover { background: var(--bg-surface-alt); color: var(--text-primary); }
.login-prompt-close svg { width: 1rem; height: 1rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; }
.login-prompt-actions { display: grid; grid-template-columns: 0.8fr 1.35fr; gap: 0.6rem; margin-top: 1.25rem; }
.login-prompt-actions button { min-height: 2.65rem; border-radius: 9px; font: inherit; font-size: 0.82rem; font-weight: 750; cursor: pointer; }
.login-prompt-cancel { border: 1px solid var(--border); background: var(--bg-surface); color: var(--text-secondary); }
.login-prompt-cancel:hover { background: var(--bg-surface-alt); color: var(--text-primary); }
.login-prompt-primary { display: inline-flex; align-items: center; justify-content: center; gap: 0.42rem; border: 1px solid var(--brand-primary); background: var(--brand-primary); color: var(--brand-on-primary); }
.login-prompt-primary:hover { background: color-mix(in srgb, var(--brand-primary) 84%, #000); }
.login-prompt-primary svg { width: 1rem; height: 1rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.login-prompt-actions button:focus-visible,
.login-prompt-close:focus-visible { outline: 3px solid color-mix(in srgb, var(--accent-link) 28%, transparent); outline-offset: 2px; }
.modal-room-detail { max-height: min(90dvh, 48rem); overflow-y: auto; border: 0; border-radius: 14px; box-shadow: none; }
.schedule-room-detail { flex: 0 0 auto; max-height: min(19rem, 46dvh); margin: -0.5rem -0.58rem 0.48rem; border: 0; border-bottom: 1px solid var(--dt-border); border-radius: 0; box-shadow: none; }
.detail-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.6rem; margin-bottom: 0.38rem; }
.detail-heading h2 { font-size: 1.14rem; }
.detail-heading-actions { display: flex; align-items: center; justify-content: flex-end; gap: 0.32rem; min-width: 0; }
.detail-close { display: inline-flex; align-items: center; justify-content: center; width: 1.8rem; height: 1.8rem; flex: 0 0 auto; padding: 0; border: 1px solid var(--dt-border); border-radius: 8px; background: var(--dt-surface); color: var(--dt-muted); cursor: pointer; transition: border-color 140ms ease-out, background-color 140ms ease-out, color 140ms ease-out; }
.detail-close svg { width: 1rem; height: 1rem; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; }
.detail-close:hover { border-color: color-mix(in srgb, var(--dt-red) 45%, var(--dt-border)); background: var(--dt-red-soft); color: var(--dt-red); }
.detail-close:focus-visible { outline: 3px solid color-mix(in srgb, var(--dt-blue) 28%, transparent); outline-offset: 2px; }
.status-badge { display: inline-flex; align-items: center; gap: 0.32rem; max-width: 55%; padding: 0.28rem 0.52rem; border-radius: 999px; background: var(--dt-surface-alt); font-size: 0.64rem; font-weight: 750; }
.status-badge.tone-free { background: var(--dt-green-soft); }
.status-badge.tone-busy { background: var(--dt-red-soft); }
.status-badge.tone-scheduled { background: var(--dt-amber-soft); }
.status-badge.tone-unknown { background: color-mix(in srgb, var(--dt-gray) 12%, var(--dt-surface)); }
.room-gallery { display: flex; flex-direction: column; gap: 0.38rem; }
.room-image { display: flex; align-items: center; justify-content: center; width: 100%; height: 8.75rem; min-height: 8.75rem; max-height: 8.75rem; overflow: hidden; border: 1px dashed color-mix(in srgb, var(--dt-blue) 22%, var(--dt-border)); border-radius: 10px; background: color-mix(in srgb, var(--dt-blue-soft) 42%, var(--dt-surface-alt)); }
.room-image-trigger { position: relative; width: 100%; height: 100%; padding: 0; overflow: hidden; border: 0; background: transparent; color: inherit; cursor: zoom-in; }
.room-image-trigger img { width: 100%; height: 100%; object-fit: cover; object-position: center; background: var(--dt-surface-alt); }
.room-image-trigger:focus-visible { outline: 2px solid var(--dt-blue); outline-offset: -3px; }
.image-zoom-hint { position: absolute; right: 0.48rem; bottom: 0.42rem; display: inline-flex; align-items: center; gap: 0.28rem; padding: 0.26rem 0.42rem; border-radius: 6px; background: rgb(15 23 42 / 0.78); color: #fff; font-size: 0.64rem; font-weight: 700; backdrop-filter: blur(5px); }
.image-zoom-hint svg { width: 0.86rem; height: 0.86rem; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; }
.room-gallery.has-thumbnails .room-image { height: 8rem; min-height: 8rem; max-height: 8rem; }
.schedule-room-detail .room-image { height: 11rem; min-height: 11rem; max-height: 11rem; }
.schedule-room-detail .room-gallery.has-thumbnails .room-image { height: 10rem; min-height: 10rem; max-height: 10rem; }
.room-thumbnails { display: flex; gap: 0.48rem; padding: 0.12rem 0.12rem 0.24rem; overflow-x: auto; scrollbar-width: thin; }
.room-thumbnail { width: 5.75rem; height: 4rem; flex: 0 0 auto; padding: 0; overflow: hidden; border: 2px solid transparent; border-radius: 9px; background: var(--dt-surface-alt); cursor: pointer; transition: border-color 140ms ease-out, transform 140ms ease-out; }
.room-thumbnail:hover { border-color: color-mix(in srgb, var(--dt-blue) 45%, var(--dt-border)); transform: translateY(-1px); }
.room-thumbnail:focus-visible { outline: 2px solid var(--dt-blue); outline-offset: 2px; }
.room-thumbnail.selected { border-color: var(--dt-blue); box-shadow: 0 2px 7px rgb(37 99 235 / 0.2); }
.room-thumbnail img { width: 100%; height: 100%; object-fit: cover; }
.room-thumbnail-more { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.05rem; border-color: color-mix(in srgb, var(--dt-blue) 24%, var(--dt-border)); background: var(--dt-blue-soft); color: var(--dt-blue); }
.room-thumbnail-more span { font-size: 1rem; font-weight: 850; line-height: 1; }
.room-thumbnail-more small { font-size: 0.62rem; font-weight: 700; }
.image-empty { display: flex; flex-direction: column; align-items: center; gap: 0.35rem; color: var(--dt-soft); font-size: 0.68rem; }
.image-empty svg { width: 2.25rem; height: 2.25rem; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }

.room-lightbox { width: min(92vw, 72rem); max-width: none; height: min(90dvh, 52rem); max-height: none; padding: 0; overflow: hidden; border: 0; border-radius: 14px; background: #0b1120; color: #fff; box-shadow: 0 24px 70px rgb(0 0 0 / 0.45); }
.room-lightbox::backdrop { background: rgb(2 6 23 / 0.82); backdrop-filter: blur(5px); }
.lightbox-shell { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; height: 100%; }
.lightbox-header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.72rem 0.82rem; background: #111827; }
.lightbox-header h2 { margin: 0; color: #fff; font-size: 1rem; }
.lightbox-header p { margin: 0.08rem 0 0; color: #cbd5e1; font-size: 0.72rem; }
.lightbox-close,
.lightbox-nav { display: inline-flex; align-items: center; justify-content: center; border: 0; color: #fff; cursor: pointer; }
.lightbox-close { width: 2.35rem; height: 2.35rem; flex: 0 0 auto; border-radius: 9px; background: rgb(255 255 255 / 0.1); }
.lightbox-close:hover { background: rgb(255 255 255 / 0.18); }
.lightbox-close:focus-visible,
.lightbox-nav:focus-visible,
.lightbox-thumbnails button:focus-visible { outline: 2px solid #93c5fd; outline-offset: 2px; }
.lightbox-close svg,
.lightbox-nav svg { width: 1.35rem; height: 1.35rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.lightbox-stage { position: relative; display: flex; align-items: center; justify-content: center; min-height: 0; padding: 0.8rem 4rem; overflow: hidden; }
.lightbox-stage > img { width: 100%; height: 100%; object-fit: contain; }
.lightbox-nav { position: absolute; z-index: 1; top: 50%; width: 2.8rem; height: 3.8rem; border-radius: 10px; background: rgb(255 255 255 / 0.12); transform: translateY(-50%); }
.lightbox-nav:hover { background: rgb(255 255 255 / 0.22); }
.lightbox-nav.previous { left: 0.7rem; }
.lightbox-nav.next { right: 0.7rem; }
.lightbox-error { color: #cbd5e1; font-size: 0.82rem; }
.lightbox-thumbnails { display: flex; justify-content: center; gap: 0.42rem; min-height: 4.7rem; padding: 0.55rem 0.75rem 0.7rem; overflow-x: auto; background: #111827; scrollbar-width: thin; }
.lightbox-thumbnails button { width: 4.7rem; height: 3.35rem; flex: 0 0 auto; padding: 0; overflow: hidden; border: 2px solid transparent; border-radius: 8px; background: #1e293b; cursor: pointer; opacity: 0.68; }
.lightbox-thumbnails button:hover { opacity: 1; }
.lightbox-thumbnails button.selected { border-color: #60a5fa; opacity: 1; }
.lightbox-thumbnails img { width: 100%; height: 100%; object-fit: cover; }
.room-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0.4rem 0 0; border-block: 1px solid var(--dt-border); }
.room-facts div { min-width: 0; padding: 0.36rem 0.28rem; text-align: center; }
.room-facts div + div { border-left: 1px solid var(--dt-border); }
.room-facts dt { color: var(--dt-soft); font-size: 0.68rem; }
.room-facts dd { margin: 0.12rem 0 0; overflow: hidden; font-size: 0.74rem; font-weight: 750; text-overflow: ellipsis; white-space: nowrap; }
.feature-section { margin-top: 0.34rem; }
.feature-heading { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.feature-heading h3 { margin: 0; font-size: 0.74rem; }
.feature-heading > span { color: var(--dt-soft); font-size: 0.68rem; }
.chip-row { display: flex; gap: 0.24rem; margin-top: 0.22rem; overflow: hidden; }
.feature-chip { display: inline-flex; align-items: center; gap: 0.28rem; min-width: 0; max-width: 8.75rem; padding: 0.2rem 0.36rem; overflow: hidden; border: 1px solid var(--dt-border); border-radius: 5px; background: var(--dt-surface-alt); color: var(--dt-muted); font-size: 0.68rem; white-space: nowrap; }
.feature-chip img { width: 1.15rem; height: 1.15rem; flex: 0 0 auto; object-fit: contain; }
.feature-chip > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.feature-chip.more { flex: 0 0 auto; color: var(--dt-blue); font-weight: 750; }
.feature-empty,
.detail-error { margin: 0.28rem 0 0; color: var(--dt-soft); font-size: 0.68rem; }
.detail-error { color: var(--dt-red); }

/* Keep the dense room-detail layout, but make its supporting text easier to read. */
.schedule-room-detail .detail-heading p { font-size: calc(0.7rem + 3px); }
.schedule-room-detail .status-badge,
.schedule-room-detail .image-zoom-hint { font-size: calc(0.64rem + 3px); }
.schedule-room-detail .room-facts dt,
.schedule-room-detail .feature-heading > span,
.schedule-room-detail .feature-chip,
.schedule-room-detail .feature-empty,
.schedule-room-detail .detail-error { font-size: calc(0.68rem + 3px); }
.schedule-room-detail .room-facts dd,
.schedule-room-detail .feature-heading h3 { font-size: calc(0.74rem + 3px); }
.schedule-room-detail .chip-row,
.modal-room-detail .chip-row {
  flex-wrap: wrap;
  align-items: flex-start;
  overflow: visible;
}
.schedule-room-detail .feature-chip,
.modal-room-detail .feature-chip {
  max-width: 100%;
  overflow: visible;
  line-height: 1.35;
  white-space: normal;
}
.schedule-room-detail .feature-chip > span,
.modal-room-detail .feature-chip > span {
  overflow: visible;
  overflow-wrap: anywhere;
  text-overflow: clip;
  white-space: normal;
}

.calendar-panel,
.schedule-panel { min-width: 0; min-height: 0; padding: 0.5rem 0.58rem; overflow: hidden; }
.calendar-panel { padding: 0.65rem 0.72rem 0.72rem; border-radius: 16px; }
.calendar-heading,
.schedule-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.6rem; margin-bottom: 0.24rem; }
.calendar-heading { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; margin-bottom: 0.55rem; }
.calendar-heading .current-time { grid-column: 1; justify-self: start; }
.calendar-heading .calendar-controls { grid-column: 2; justify-self: center; }
.calendar-heading .calendar-actions { grid-column: 3; justify-self: end; }
.calendar-actions { display: flex; align-items: center; justify-content: flex-end; gap: 0.36rem; }
.calendar-title-group,
.status-title-group { display: flex; align-items: center; gap: 0.62rem; min-width: 0; }
.calendar-title-group > div,
.status-title-group > div { min-width: 0; }
.calendar-icon,
.status-icon { display: inline-flex; align-items: center; justify-content: center; width: 2.45rem; height: 2.45rem; flex: 0 0 auto; border-radius: 11px; background: var(--dt-blue-soft); color: var(--dt-blue); }
.calendar-icon svg,
.status-icon svg { width: 1.45rem; height: 1.45rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.calendar-heading h2 { font-size: 1.02rem; }
.status-title-group h2 { font-size: 1.2rem; }
.calendar-heading h2 span { color: var(--dt-blue); }
.calendar-controls { display: flex; align-items: center; gap: 0.28rem; }
.calendar-month-picker { position: relative; display: inline-flex; }
.calendar-controls .calendar-month-button { width: auto; min-width: 7.2rem; padding-inline: 0.5rem; border-color: transparent; background: transparent; color: var(--dt-text); font-size: 0.78rem; font-weight: 800; white-space: nowrap; }
.calendar-controls .calendar-month-button:hover { border-color: var(--dt-border); }
.native-date-picker { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; pointer-events: none; }
.calendar-controls button,
.calendar-today-button { display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border: 1px solid var(--dt-border); border-radius: 9px; background: var(--dt-surface); color: var(--dt-text); font: inherit; cursor: pointer; transition: border-color 140ms ease-out, background-color 140ms ease-out; }
.calendar-controls button:hover,
.calendar-today-button:hover { border-color: color-mix(in srgb, var(--dt-blue) 48%, var(--dt-border)); background: var(--dt-blue-soft); }
.calendar-today-button { width: auto; padding-inline: 0.68rem; background: var(--dt-blue-soft); color: var(--dt-blue); font-size: 0.72rem; font-weight: 800; }
.view-switch-button { display: inline-flex; align-items: center; justify-content: center; gap: 0.32rem; min-height: 2rem; padding: 0.34rem 0.58rem; border: 1px solid color-mix(in srgb, var(--dt-blue) 30%, var(--dt-border)); border-radius: 9px; background: var(--dt-surface); color: var(--dt-blue); font: inherit; font-size: calc(0.72rem + 2px); font-weight: 800; white-space: nowrap; cursor: pointer; transition: border-color 140ms ease-out, background-color 140ms ease-out, box-shadow 140ms ease-out, transform 140ms ease-out; }
.view-switch-button svg { width: 1rem; height: 1rem; flex: 0 0 auto; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.view-switch-button:hover { border-color: color-mix(in srgb, var(--dt-blue) 58%, var(--dt-border)); background: var(--dt-blue-soft); box-shadow: 0 2px 7px color-mix(in srgb, var(--dt-blue) 13%, transparent); transform: translateY(-1px); }
.view-switch-button:focus-visible { outline: 3px solid color-mix(in srgb, var(--dt-blue) 28%, transparent); outline-offset: 2px; }
.calendar-controls svg { width: 1rem; height: 1rem; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
.calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); grid-template-rows: minmax(2.1rem, auto) repeat(5, minmax(1.55rem, 1fr)); gap: 0.28rem; height: calc(100% - 3.25rem); min-height: 0; }
.weekday { display: flex; flex-direction: column; align-items: center; justify-content: center; min-width: 0; padding: 0.24rem 0.15rem; border-radius: 10px; background: var(--dt-blue-soft); color: var(--dt-text); text-align: center; }
.weekday b { font-size: 0.76rem; line-height: 1; }
.weekday.sunday { background: var(--dt-red-soft); color: var(--dt-red); }
.weekday.saturday { background: var(--dt-green-soft); color: var(--dt-green); }
.day-cell { position: relative; display: flex; align-items: center; justify-content: center; min-width: 0; min-height: 0; border: 1px solid var(--dt-border); border-radius: 10px; background: var(--dt-surface); color: var(--dt-text); font: inherit; font-size: 0.86rem; font-weight: 700; font-variant-numeric: tabular-nums; cursor: pointer; transition: border-color 140ms ease-out, background-color 140ms ease-out, box-shadow 140ms ease-out, transform 140ms ease-out; }
.day-cell:hover { border-color: color-mix(in srgb, var(--dt-blue) 55%, var(--dt-border)); background: var(--dt-blue-soft); transform: translateY(-1px); }
.day-cell.muted { color: var(--dt-soft); opacity: 0.5; }
.day-cell.sunday:not(.selected):not(.muted) { color: var(--dt-red); }
.day-cell.saturday:not(.selected):not(.muted) { color: var(--dt-green); }
.day-cell.today { border-width: 2px; border-color: var(--dt-blue); background: var(--dt-blue-soft); color: var(--dt-blue); font-weight: 850; }
.day-cell.today.selected { background: var(--dt-blue); color: #fff; box-shadow: inset 0 0 0 2px rgb(255 255 255 / 0.78), 0 3px 9px rgb(37 99 235 / 0.24); }
.day-cell.selected { border-color: var(--dt-blue); background: var(--dt-blue); color: #fff; box-shadow: 0 3px 9px rgb(37 99 235 / 0.24); }
:global(:root[data-theme="dark"]) .day-cell.selected { color: #fff; }
.day-cell i { position: absolute; right: 0.36rem; bottom: 0.28rem; min-width: 0.38rem; width: 0.38rem; height: 0.38rem; border-radius: 50%; background: var(--dt-amber); color: transparent; font-size: 0; font-style: normal; line-height: 0; }
.day-cell.selected i { background: #fff; }

.schedule-heading > span { padding: 0.24rem 0.42rem; border-radius: 999px; background: var(--dt-blue-soft); color: var(--dt-blue); font-size: 0.6rem; font-weight: 750; white-space: nowrap; }
.schedule-panel { display: flex; flex-direction: column; }
.schedule-panel.has-room-detail .schedule-room-detail { flex: 1 1 0; max-height: none; }
.schedule-panel.has-room-detail .schedule-table { flex: 0 0 11rem; height: 11rem; max-height: 11rem; }
.schedule-panel.has-room-detail .schedule-empty { flex: 0 0 11rem; height: 11rem; min-height: 11rem; max-height: 11rem; }
.schedule-title-group { display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
.schedule-icon { display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; flex: 0 0 auto; border-radius: 9px; background: var(--dt-blue-soft); color: var(--dt-blue); }
.schedule-icon svg { width: 1.2rem; height: 1.2rem; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
.schedule-table { display: flex; flex: 1; min-height: 0; flex-direction: column; overflow: hidden; border: 1px solid var(--dt-border); border-radius: 9px; background: var(--dt-surface); }
.schedule-table-head,
.schedule-table-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 0.78fr) minmax(0, 2.75fr) minmax(0, 0.9fr); align-items: center; min-width: 0; }
.schedule-table-head { flex: 0 0 auto; background: var(--dt-blue-soft); color: var(--dt-soft); font-size: 0.8rem; font-weight: 800; }
.schedule-table-head span { padding: 0.42rem 0.44rem; overflow: hidden; text-align: center; text-overflow: ellipsis; white-space: nowrap; }
.schedule-table-head > * + * { border-left: 1px solid color-mix(in srgb, var(--dt-border) 70%, transparent); }
.schedule-table-body { flex: 1 1 auto; min-height: 0; overflow-x: hidden; overflow-y: auto; scrollbar-width: thin; }
.schedule-table-row { border-bottom: 1px solid var(--dt-border); font-size: 0.86rem; }
.schedule-table-row.past { background: color-mix(in srgb, var(--dt-surface-alt) 62%, var(--dt-page)); opacity: 0.45; filter: grayscale(0.7) saturate(0.12); }
.schedule-table-row > * { min-width: 0; padding: 0.42rem 0.44rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.schedule-table-row > * + * { border-left: 1px solid color-mix(in srgb, var(--dt-border) 70%, transparent); }
.schedule-table-row time { justify-self: start; margin-left: 0.32rem; padding: 0.22rem 0.34rem; border-radius: 6px; background: var(--dt-blue-soft); color: var(--dt-blue); font-size: 0.78rem; font-weight: 850; font-variant-numeric: tabular-nums; }
.schedule-room { color: var(--dt-text); font-size: 0.86rem; font-weight: 800; }
.schedule-subject { display: flex; flex-direction: column; align-items: flex-start; gap: 0.08rem; border-right: 1px solid color-mix(in srgb, var(--dt-border) 70%, transparent); line-height: 1.25; }
.schedule-course { display: block; max-width: 100%; overflow: hidden; color: var(--dt-text); font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.schedule-owner { display: block; max-width: 100%; overflow: hidden; color: var(--dt-soft); font-size: 0.76rem; font-weight: 400; text-overflow: ellipsis; white-space: nowrap; }
.schedule-state { justify-self: center; padding: 0.22rem 0.36rem; border-left: 0; border-radius: 999px; font-size: 0.78rem; font-weight: 750; white-space: nowrap; }
.schedule-state.confirmed { background: var(--dt-green-soft); color: var(--dt-green); }
.schedule-state.closed { background: var(--dt-red-soft); color: var(--dt-red); }
.schedule-state.scheduled { background: var(--dt-amber-soft); color: var(--dt-amber); }

.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

@media (min-width: 1200px) and (min-height: 700px) {
  .dashboard-test-page { min-height: 0; }
}

@media (min-width: 1200px) and (min-height: 850px) {
  .calendar-grid { grid-template-rows: minmax(2.6rem, auto) repeat(5, minmax(2.15rem, 1fr)); }
}

@media (max-width: 1199px) {
  .dashboard-test-page { height: auto; min-height: calc(100dvh - 3.1rem); overflow: visible; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto auto; }
  .overview-grid,
  .lower-grid { display: grid; min-width: 0; min-height: 0; gap: 0.46rem; }
  .overview-main { display: grid; grid-template-rows: auto auto; gap: 0.4rem; min-width: 0; min-height: 0; }
  .overview-grid { grid-template-columns: minmax(0, 1fr); }
  .lower-grid { grid-template-columns: minmax(0, 1fr); }
  .kpi-grid,
  .status-panel,
  .calendar-panel-expanded { grid-column: auto; grid-row: auto; }
  .schedule-panel { grid-column: 1; grid-row: auto; }
  .overview-main { min-height: 31rem; }
  .room-detail-panel { max-height: 31rem; }
  .lower-grid { min-height: 18rem; }
}

@media (max-width: 900px) {
  .overview-grid,
  .lower-grid { grid-template-columns: 1fr; }
  .schedule-panel { grid-column: 1; }
  .overview-main { min-height: 34rem; }
  .room-detail-panel { max-height: none; }
  .calendar-panel { min-height: 23rem; }
  .schedule-panel { min-height: 12rem; }
}

@media (max-width: 620px) {
  .dashboard-test-page { gap: 0.36rem; min-height: calc(100dvh - 3.35rem); padding: 0.34rem; font-size: 15px; }
  .desktop-room-detail { display: none; }
  .overview-main { min-height: 0; grid-template-rows: auto auto; }
  .overview-grid,
  .overview-main,
  .lower-grid { gap: 0.34rem; }
  .mobile-current-time {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.38rem;
    width: 100%;
    padding: 0.3rem 0;
    text-align: center;
  }
  .time-strip { min-height: 2.7rem; padding: 0.26rem 0.42rem; }
  .current-time { gap: 0.38rem; }
  .time-icon { width: 1.8rem; height: 1.8rem; border-radius: 8px; }
  .time-icon svg { width: 1.08rem; height: 1.08rem; }
  .date-label { font-size: calc(0.66rem + 1px); }
  .time-label { font-size: calc(1.35rem + 1px); }
  .live-indicator { gap: 0.26rem; font-size: calc(0.62rem + 1px); }
  .live-dot { width: 0.42rem; height: 0.42rem; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.3rem; }
  .kpi-card { grid-template-columns: 2rem minmax(0, 1fr); gap: 0.42rem; min-height: 4rem; padding: 0.38rem 0.5rem; }
  .kpi-icon { width: 1.95rem; height: 1.95rem; border-radius: 8px; }
  .kpi-icon svg { width: 1.2rem; height: 1.2rem; }
  .kpi-card p { font-size: calc(0.72rem + 1px); }
  .kpi-card strong { font-size: calc(1.25rem + 1px); }
  .kpi-card strong small { font-size: calc(0.7rem + 1px); }
  .kpi-card div > span { font-size: calc(0.68rem + 1px); }
  .status-panel { min-height: 0; height: auto; padding: 0.44rem; overflow: visible; border-radius: 12px; }
  .section-heading { flex-direction: column; align-items: stretch; }
  .section-heading { gap: 0.4rem; margin-bottom: 0.4rem; }
  .status-panel > .section-heading { grid-template-columns: minmax(0, 1fr); gap: 0.4rem; }
  .status-current-time,
  .calendar-heading .current-time { display: none; }
  .status-title-group { grid-column: 1; grid-row: 1; justify-self: center; }
  .status-heading-side { grid-column: 1; grid-row: 2; width: 100%; flex-direction: row; align-items: center; justify-content: space-between; }
  .status-title-group h2 { font-size: calc(1rem + 1px); }
  .view-switch-button { min-height: 1.72rem; padding: 0.22rem 0.4rem; border-radius: 7px; font-size: calc(0.7rem + 1px); }
  .view-switch-button svg { width: 0.82rem; height: 0.82rem; }
  .status-legend { justify-content: flex-start; gap: 0.18rem 0.42rem; font-size: calc(0.68rem + 1px); }
  .status-legend i,
  .status-badge i { width: 0.36rem; height: 0.36rem; }
  .room-floor-groups { max-height: none; overflow: visible; }
  .room-floor-section + .room-floor-section { margin-top: 0.5rem; }
  .room-floor-heading { position: static; gap: 0.4rem; margin-bottom: 0.26rem; padding: 0.14rem 0.05rem 0.24rem; }
  .room-floor-heading h3 { font-size: calc(0.78rem + 1px); }
  .room-floor-heading > span { padding: 0.12rem 0.3rem; font-size: calc(0.62rem + 1px); }
  .room-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 0.34rem; row-gap: 0.36rem; padding: 0.1rem 0.1rem 0.22rem; }
  .room-card { min-height: 4.65rem; padding: 0.4rem 0.46rem; border-radius: 8px; }
  .room-card-head { gap: 0.3rem; }
  .room-card-head-actions { gap: 0.25rem; }
  .room-title { gap: 0.24rem; }
  .room-card-head strong { font-size: calc(0.82rem + 1px); }
  .status-dot { width: 0.42rem; height: 0.42rem; }
  .room-state-text { font-size: calc(0.7rem + 1px); }
  .room-context,
  .room-subject,
  .room-instructor,
  .people-count { font-size: calc(0.66rem + 1px); }
  .room-card-actions { gap: 0.22rem; margin-top: 0.24rem; }
  .room-detail-button,
  .room-book-button { min-height: 2rem; padding: 0.2rem 0.36rem; border-radius: 5px; font-size: calc(0.66rem + 1px); }
  .room-detail-button { display: inline-flex; width: 1.85rem; min-width: 1.85rem; min-height: 1.85rem; padding: 0; }
  .room-detail-button svg { width: 1.1rem; height: 1.1rem; }
  .room-detail-dialog {
    inset: 0;
    width: 100vw;
    max-width: none;
    height: 100dvh;
    max-height: none;
    margin: 0;
    border: 0;
    border-radius: 0;
  }
  .modal-room-detail {
    width: 100%;
    height: 100%;
    max-height: none;
    padding-top: max(0.58rem, env(safe-area-inset-top));
    padding-right: max(0.58rem, env(safe-area-inset-right));
    padding-bottom: max(0.75rem, env(safe-area-inset-bottom));
    padding-left: max(0.58rem, env(safe-area-inset-left));
    border-radius: 0;
    box-sizing: border-box;
  }
  .room-detail-panel { overflow: visible; }
  .room-detail-dialog .modal-room-detail { overflow-y: auto; }
  .room-detail-panel { padding: 0.44rem; }
  .detail-heading { gap: 0.4rem; margin-bottom: 0.28rem; }
  .detail-heading h2 { font-size: calc(0.94rem + 1px); }
  .detail-heading p { font-size: calc(0.68rem + 1px); }
  .detail-close { width: 1.65rem; height: 1.65rem; border-radius: 7px; }
  .status-badge { gap: 0.24rem; padding: 0.2rem 0.4rem; font-size: calc(0.6rem + 1px); }
  .schedule-room-detail { max-height: none; margin: -0.38rem -0.42rem 0.36rem; }
  .room-image,
  .room-image img { height: 7rem; min-height: 7rem; max-height: 7rem; }
  .room-gallery.has-thumbnails .room-image { height: 6.5rem; min-height: 6.5rem; max-height: 6.5rem; }
  .room-detail-dialog .modal-room-detail .room-image,
  .room-detail-dialog .modal-room-detail .room-image img {
    height: clamp(14rem, 34dvh, 22rem);
    min-height: clamp(14rem, 34dvh, 22rem);
    max-height: clamp(14rem, 34dvh, 22rem);
  }
  .room-detail-dialog .modal-room-detail .room-gallery.has-thumbnails .room-image {
    height: clamp(13rem, 32dvh, 20rem);
    min-height: clamp(13rem, 32dvh, 20rem);
    max-height: clamp(13rem, 32dvh, 20rem);
  }
  .schedule-room-detail .room-image { height: 8rem; min-height: 8rem; max-height: 8rem; }
  .schedule-room-detail .room-gallery.has-thumbnails .room-image { height: 7.5rem; min-height: 7.5rem; max-height: 7.5rem; }
  .room-thumbnail { width: 5.5rem; height: 3.8rem; border-radius: 7px; }
  .room-thumbnail-more span { font-size: calc(1rem + 1px); }
  .room-thumbnail-more small { font-size: calc(0.62rem + 1px); }
  .room-facts { margin-top: 0.3rem; }
  .room-facts div { padding: 0.28rem 0.2rem; }
  .room-facts dt { font-size: calc(0.62rem + 1px); }
  .room-facts dd { font-size: calc(0.68rem + 1px); }
  .feature-section { margin-top: 0.26rem; }
  .feature-heading h3 { font-size: calc(0.68rem + 1px); }
  .feature-heading > span,
  .feature-chip,
  .feature-empty,
  .detail-error { font-size: calc(0.62rem + 1px); }
  .feature-chip { padding: 0.16rem 0.28rem; }
  .feature-chip img { width: 1rem; height: 1rem; }
  .schedule-room-detail .detail-heading p,
  .schedule-room-detail .status-badge,
  .schedule-room-detail .image-zoom-hint,
  .schedule-room-detail .room-facts dt,
  .schedule-room-detail .feature-heading > span,
  .schedule-room-detail .feature-chip,
  .schedule-room-detail .feature-empty,
  .schedule-room-detail .detail-error,
  .modal-room-detail .feature-heading > span,
  .modal-room-detail .feature-chip,
  .modal-room-detail .feature-empty,
  .modal-room-detail .detail-error { font-size: calc(0.66rem + 1px); }
  .schedule-room-detail .room-facts dd,
  .schedule-room-detail .feature-heading h3,
  .modal-room-detail .feature-heading h3 { font-size: calc(0.72rem + 1px); }
  .modal-room-detail .detail-heading h2 { font-size: calc(1.06rem + 1px); }
  .modal-room-detail .detail-heading p { font-size: calc(0.8rem + 1px); }
  .modal-room-detail .status-badge,
  .modal-room-detail .image-zoom-hint { font-size: calc(0.74rem + 1px); }
  .modal-room-detail .room-facts dt { font-size: calc(0.74rem + 1px); }
  .modal-room-detail .room-facts dd { font-size: calc(0.8rem + 1px); }
  .modal-room-detail .feature-heading h3 { font-size: calc(0.84rem + 1px); }
  .modal-room-detail .feature-heading > span,
  .modal-room-detail .feature-chip,
  .modal-room-detail .feature-empty,
  .modal-room-detail .detail-error { font-size: calc(0.78rem + 1px); }
  .modal-room-detail .feature-chip { padding: 0.24rem 0.36rem; line-height: 1.4; }
  .modal-room-detail .feature-chip img { width: 1.15rem; height: 1.15rem; }
  .room-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .room-facts div:nth-child(3) { border-left: 0; border-top: 1px solid var(--dt-border); }
  .room-facts div:nth-child(4) { border-top: 1px solid var(--dt-border); }
  .calendar-panel { min-height: 19rem; padding: 0.44rem; border-radius: 12px; }
  .calendar-heading { grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; }
  .calendar-heading { gap: 0.4rem; margin-bottom: 0.38rem; }
  .calendar-heading .calendar-controls { grid-column: 2; grid-row: 1; justify-self: center; }
  .calendar-heading .calendar-actions { grid-column: 3; grid-row: 1; justify-self: end; }
  .calendar-icon,
  .status-icon { display: none; }
  .calendar-heading h2 { font-size: calc(0.82rem + 1px); }
  .calendar-controls { gap: 0.2rem; }
  .calendar-controls button { width: 1.65rem; height: 1.65rem; border-radius: 7px; }
  .calendar-controls .calendar-month-button { min-width: 6rem; padding-inline: 0.35rem; font-size: calc(0.7rem + 1px); }
  .calendar-today-button { height: 1.65rem; padding-inline: 0.4rem; font-size: calc(0.66rem + 1px); }
  .calendar-grid { grid-template-rows: 1.8rem repeat(5, minmax(1.85rem, 1fr)); gap: 0.16rem; height: calc(100% - 2.7rem); }
  .weekday { padding: 0.18rem 0.1rem; border-radius: 7px; }
  .weekday b { font-size: calc(0.68rem + 1px); }
  .day-cell { border-radius: 7px; font-size: calc(0.7rem + 1px); }
  .day-cell i { right: 0.24rem; bottom: 0.2rem; width: 0.3rem; height: 0.3rem; min-width: 0.3rem; }
  .schedule-panel { min-height: 14rem; padding: 0.4rem 0.44rem; }
  .schedule-heading { gap: 0.4rem; margin-bottom: 0.2rem; }
  .schedule-heading h2 { font-size: calc(0.82rem + 1px); }
  .schedule-heading p { font-size: calc(0.68rem + 1px); }
  .schedule-heading > span { padding: 0.18rem 0.32rem; font-size: calc(0.56rem + 1px); }
  .schedule-icon { width: 1.7rem; height: 1.7rem; border-radius: 7px; }
  .schedule-icon svg { width: 1rem; height: 1rem; }
  .schedule-panel.has-room-detail .schedule-room-detail { flex: 0 0 auto; }
  .schedule-panel.has-room-detail .schedule-table { flex: 0 1 auto; height: auto; max-height: none; }
  .schedule-panel.has-room-detail .schedule-empty { flex: 0 0 5.5rem; height: auto; min-height: 5.5rem; max-height: none; }
  .schedule-table { overflow: visible; border: 0; background: transparent; }
  .schedule-table-head { display: none; }
  .schedule-table-body { display: flex; flex-direction: column; gap: 0.24rem; overflow: visible; }
  .schedule-table-row { grid-template-columns: minmax(0, 1fr) auto; gap: 0.08rem 0.38rem; padding: 0.32rem 0.38rem; border: 1px solid var(--dt-border); border-radius: 7px; background: var(--dt-surface-alt); font-size: calc(0.74rem + 1px); }
  .schedule-table-row > * { padding: 0; border-left: 0; }
  .schedule-table-row time { grid-column: 1; grid-row: 1; justify-self: start; margin: 0; padding: 0.16rem 0.26rem; font-size: calc(0.68rem + 1px); }
  .schedule-room { grid-column: 2; grid-row: 1; font-size: calc(0.74rem + 1px); text-align: right; }
  .schedule-subject { grid-column: 1 / -1; grid-row: 2; border-right: 0; }
  .schedule-course { font-size: calc(0.76rem + 1px); }
  .schedule-owner { font-size: calc(0.66rem + 1px); }
  .schedule-state { grid-column: 2; grid-row: 3; padding: 0.16rem 0.28rem; font-size: calc(0.66rem + 1px); }
}

@media (max-width: 390px) {
  .dashboard-test-page { font-size: 14px; }
  .kpi-card { grid-template-columns: 1.9rem minmax(0, 1fr); gap: 0.38rem; padding-inline: 0.42rem; }
  .kpi-icon { width: 1.85rem; height: 1.85rem; }
  .kpi-card strong { font-size: calc(1.18rem + 1px); }
  .room-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .status-panel { min-height: 0; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
</style>
