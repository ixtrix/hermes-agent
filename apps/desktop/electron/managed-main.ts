import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { app, BrowserWindow, dialog, ipcMain, Menu, Notification, safeStorage, session, shell, type WebContents } from 'electron'

import { buildGatewayWsUrlWithTicket, normalizeRemoteBaseUrl } from './connection-config'
import { ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES, resolveReadableFileForIpc } from './hardening'
import { MANAGED_PRODUCT_PROTOCOLS, managedProductPolicy, managedUserDataRoot } from './managed-product'
import { type NativeTokenSet, parseStoredTokenSet, parseTokenResponse, tokenNeedsRefresh } from './native-oauth'
import { runNativeLogin } from './native-oauth-login'

if (!managedProductPolicy) {
  throw new Error('Managed Electron main requires an embedded product policy.')
}

const PRODUCT = managedProductPolicy
const PROTOCOL = MANAGED_PRODUCT_PROTOCOLS[PRODUCT.plane]
const APP_ROOT = app.getAppPath()
const PRELOAD_PATH = path.join(APP_ROOT, 'dist', 'electron-preload.js')
const RENDERER_PATH = path.join(APP_ROOT, 'dist', 'index.html')
const EXPORT_ID_RE = /^exp_[A-Za-z0-9_-]{43}$/
const MEDIA_MIME_TYPES: Record<string, string> = {
  '.avi': 'video/x-msvideo',
  '.bmp': 'image/bmp',
  '.flac': 'audio/flac',
  '.gif': 'image/gif',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.m4a': 'audio/mp4',
  '.mkv': 'video/x-matroska',
  '.mov': 'video/quicktime',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
  '.ogg': 'audio/ogg',
  '.opus': 'audio/ogg; codecs=opus',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.wav': 'audio/wav',
  '.webm': 'video/webm',
  '.webp': 'image/webp'
}

type ManagedAttachmentPurpose = 'document' | 'media' | 'supplier'

interface ManagedSupplierMetadata {
  supplier_domain: string
  supplier_id: string
}
interface ManagedAttachmentPickerOptions {
  filters?: Array<{ extensions: string[]; name: string }>
  multiple?: boolean
  purpose?: unknown
  supplier?: unknown
  title?: string
}


function parseManagedAttachmentOptions(options: ManagedAttachmentPickerOptions | undefined): {
  purpose: ManagedAttachmentPurpose
  supplier?: ManagedSupplierMetadata
} {
  const purpose = options?.purpose

  if (purpose !== 'document' && purpose !== 'media' && purpose !== 'supplier') {
    throw new Error('Managed attachment purpose must be document, supplier, or media.')
  }

  const supplier = options?.supplier

  if (purpose !== 'supplier') {
    if (supplier !== undefined) {
      throw new Error('Supplier metadata is only valid for supplier attachments.')
    }

    return { purpose }
  }

  if (
    !supplier ||
    typeof supplier !== 'object' ||
    !('supplier_id' in supplier) ||
    !('supplier_domain' in supplier) ||
    typeof supplier.supplier_id !== 'string' ||
    !supplier.supplier_id.trim() ||
    typeof supplier.supplier_domain !== 'string' ||
    !supplier.supplier_domain.trim()
  ) {
    throw new Error('Supplier attachments require explicit supplier metadata.')
  }

  return {
    purpose,
    supplier: {
      supplier_domain: supplier.supplier_domain.trim(),
      supplier_id: supplier.supplier_id.trim()
    }
  }
}
function mimeTypeForPath(filePath: string): string {
  return MEDIA_MIME_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream'
}

const DEFAULT_TIMEOUT_MS = 30_000

app.setName(PRODUCT.productName)
const managedStateBase = process.platform === 'win32' ? process.env.LOCALAPPDATA : app.getPath('appData')
app.setPath('userData', path.resolve(managedUserDataRoot(managedStateBase, PRODUCT.plane, process.platform)))

const TOKEN_PATH = path.join(app.getPath('userData'), 'native-token.json')
let mainWindow: BrowserWindow | null = null
let tokenCache: NativeTokenSet | null | undefined
let connectionPromise: Promise<any> | null = null
let bootProgress = {
  error: null as string | null,
  message: 'Connecting to managed Hermes…',
  phase: 'starting',
  progress: 0,
  running: true
}

function setBootProgress(patch: Partial<typeof bootProgress>) {
  bootProgress = { ...bootProgress, ...patch }
  mainWindow?.webContents.send('hermes:boot-progress', bootProgress)
}

function loadTokens(): NativeTokenSet | null {
  if (tokenCache !== undefined) {
    return tokenCache
  }

  try {
    const encoded = JSON.parse(fs.readFileSync(TOKEN_PATH, 'utf8')).encrypted
    tokenCache = parseStoredTokenSet(JSON.parse(safeStorage.decryptString(Buffer.from(encoded, 'base64'))))
  } catch {
    tokenCache = null
  }

  return tokenCache
}

function storeTokens(tokens: NativeTokenSet) {
  tokenCache = tokens
  fs.mkdirSync(path.dirname(TOKEN_PATH), { recursive: true })
  const encrypted = safeStorage.encryptString(JSON.stringify(tokens)).toString('base64')
  fs.writeFileSync(TOKEN_PATH, JSON.stringify({ encrypted }), { mode: 0o600 })
}

function clearTokens() {
  tokenCache = null
  try {
    fs.rmSync(TOKEN_PATH, { force: true })
  } catch {
    // Already logged out.
  }
}

async function jsonRequest(url: string, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const headers = new Headers(init.headers)
  headers.set('accept', 'application/json')
  const tokens = loadTokens()

  if (tokens?.accessToken) {
    headers.set('authorization', `Bearer ${tokens.accessToken}`)
  }

  try {
    const response = await fetch(url, { ...init, headers, redirect: 'error', signal: controller.signal })
    const text = await response.text()
    let body: any = null

    try {
      body = text ? JSON.parse(text) : null
    } catch {
      body = text
    }

    if (!response.ok) {
      const error: any = new Error(`${response.status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`)
      error.statusCode = response.status
      throw error
    }

    return { body, headers: response.headers }
  } finally {
    clearTimeout(timer)
  }
}

async function accessToken(): Promise<string | null> {
  const tokens = loadTokens()

  if (!tokens) {
    return null
  }

  if (!tokenNeedsRefresh(tokens, Math.floor(Date.now() / 1000))) {
    return tokens.accessToken
  }

  if (!tokens.refreshToken) {
    clearTokens()
    return null
  }

  try {
    const { body } = await jsonRequest(
      `${PRODUCT.origin}/auth/native/refresh`,
      { method: 'POST', body: JSON.stringify({ refresh_token: tokens.refreshToken }) },
      15_000
    )
    const refreshed = parseTokenResponse(body)
    storeTokens(refreshed)
    return refreshed.accessToken
  } catch {
    clearTokens()
    return null
  }
}

async function mintWsTicket(): Promise<string> {
  const token = await accessToken()

  if (!token) {
    throw new Error('Managed Hermes sign-in is required.')
  }

  const { body } = await jsonRequest(
    `${PRODUCT.origin}/api/auth/ws-ticket`,
    { method: 'POST', headers: { authorization: `Bearer ${token}` } },
    15_000
  )
  const ticket = String(body?.ticket || '')

  if (!ticket) {
    throw new Error('Managed Hermes gateway did not issue a WebSocket ticket.')
  }

  return ticket
}

async function ensureConnection() {
  if (!connectionPromise) {
    connectionPromise = (async () => {
      setBootProgress({ message: 'Connecting to managed Hermes…', phase: 'remote.connect', progress: 24 })
      const origin = normalizeRemoteBaseUrl(PRODUCT.origin)
      const token = await accessToken()

      if (!token) {
        throw new Error('Managed Hermes sign-in is required.')
      }

      await jsonRequest(`${origin}/api/status`, { headers: { authorization: `Bearer ${token}` } }, 15_000)
      const ticket = await mintWsTicket()
      const connection = {
        authMode: 'oauth',
        baseUrl: origin,
        mode: 'remote',
        plane: PRODUCT.plane,
        productId: PRODUCT.productId,
        profile: 'default',
        remoteAuthMode: 'oauth',
        remoteUrl: origin,
        source: 'managed',
        token: null,
        wsUrl: buildGatewayWsUrlWithTicket(origin, ticket)
      }
      setBootProgress({ message: 'Managed Hermes is ready', phase: 'backend.ready', progress: 100, running: false })
      return connection
    })().catch(error => {
      connectionPromise = null
      setBootProgress({ error: error instanceof Error ? error.message : String(error), message: 'Managed Hermes could not connect', phase: 'error', progress: 0, running: false })
      throw error
    })
  }

  return connectionPromise
}

async function apiRequest(request: any) {
  const requestPath = String(request?.path || '')

  if (!/^\/(?:api|v1)(?:\/|$)/.test(requestPath) || request?.upload) {
    throw new Error('Managed Hermes only permits remote JSON API requests.')
  }

  const connection = await ensureConnection()
  const body = request?.body == null ? undefined : typeof request.body === 'string' ? request.body : JSON.stringify(request.body)
  const { body: responseBody } = await jsonRequest(
    `${connection.baseUrl}${requestPath}`,
    { body, method: String(request?.method || 'GET').toUpperCase(), headers: body ? { 'content-type': 'application/json' } : undefined },
    Number(request?.timeoutMs) || DEFAULT_TIMEOUT_MS
  )

  return responseBody
}

function safeExportFilename(headers: Headers, fallback: unknown): string {
  const disposition = headers.get('content-disposition') || ''
  const match = /filename\*?=(?:UTF-8'')?("?)([^";]+)\1/i.exec(disposition)
  let candidate = match ? match[2] : String(fallback || `export-${Date.now()}`)

  try {
    candidate = decodeURIComponent(candidate)
  } catch {
    // Keep the raw header value when it is not encoded text.
  }

  const safe = [...path.basename(candidate)]
    .map(char => (char.charCodeAt(0) < 32 || '\\/:*?"<>|'.includes(char) ? '_' : char))
    .join('')
    .trim()

  return safe || `export-${Date.now()}`
}

async function saveManagedExport(exportId: unknown, suggestedName: unknown) {
  const id = String(exportId || '').trim()

  if (!EXPORT_ID_RE.test(id)) {
    throw new Error('Export id is invalid.')
  }

  const result = await dialog.showSaveDialog(mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined, {
    defaultPath: safeExportFilename(new Headers(), suggestedName),
    title: 'Save Hermes export'
  })

  if (result.canceled || !result.filePath) {
    return { canceled: true, ok: false }
  }

  const connection = await ensureConnection()
  const token = await accessToken()
  const response = await fetch(`${connection.baseUrl}/v1/exports/${encodeURIComponent(id)}/download`, {
    headers: token ? { authorization: `Bearer ${token}` } : undefined,
    redirect: 'error'
  })

  const body = new Uint8Array(await response.arrayBuffer())

  if (!response.ok) {
    throw new Error(`Export download failed (${response.status}).`)
  }

  const contentLength = response.headers.get('Content-Length') || ''
  const digest = (response.headers.get('X-Content-SHA256') || '').toLowerCase()
  const actualDigest = crypto.createHash('sha256').update(body).digest('hex')

  if (!/^\d+$/.test(contentLength) || Number(contentLength) !== body.byteLength) {
    throw new Error('Export download length verification failed.')
  }
  if (!/^[a-f0-9]{64}$/.test(digest) || digest !== actualDigest) {
    throw new Error('Export download integrity verification failed.')
  }

  const directory = path.dirname(result.filePath)
  const temporaryPath = path.join(directory, `.hermes-export-${process.pid}-${crypto.randomBytes(8).toString('hex')}.tmp`)

  try {
    await fs.promises.writeFile(temporaryPath, body, { flag: 'wx', mode: 0o600 })
    await fs.promises.rename(temporaryPath, result.filePath)
  } finally {
    await fs.promises.rm(temporaryPath, { force: true }).catch(() => undefined)
  }

  return { filePath: result.filePath, ok: true }
}

async function pickAttachmentBytes(_event: unknown, rawOptions: unknown = {}) {
  const options =
    rawOptions && typeof rawOptions === 'object' ? (rawOptions as ManagedAttachmentPickerOptions) : undefined
  const { purpose, supplier } = parseManagedAttachmentOptions(options)
  const result = await dialog.showOpenDialog(mainWindow, {
    filters: Array.isArray(options?.filters) ? options.filters : undefined,
    properties: options?.multiple === false ? ['openFile'] : ['openFile', 'multiSelections'],
    title: typeof options?.title === 'string' && options.title ? options.title : 'Add attachment'
  })

  if (result.canceled) {
    return []
  }

  return Promise.all(
    result.filePaths.map(async filePath => {
      const { resolvedPath, stat } = await resolveReadableFileForIpc(filePath, {
        maxBytes: ATTACHMENT_UPLOAD_DEFAULT_MAX_BYTES,
        purpose: 'Managed attachment picker'
      })
      const bytes = await fs.promises.readFile(resolvedPath)

      if (bytes.byteLength !== stat.size) {
        throw new Error(`Attachment changed while reading: ${path.basename(resolvedPath)}`)
      }

      return {
        bytes: new Uint8Array(bytes),
        mime: mimeTypeForPath(resolvedPath),
        name: path.basename(resolvedPath),
        purpose,
        size: bytes.byteLength,
        ...(supplier ? { supplier } : {})
      }
    })
  )
}

function connectionConfig() {
  return {
    mode: 'remote',
    oidc: PRODUCT.oidc,
    plane: PRODUCT.plane,
    productId: PRODUCT.productId,
    profile: 'default',
    remoteAuthMode: 'oauth',
    remoteOauthConnected: Boolean(loadTokens()),
    remoteTokenPreview: null,
    remoteTokenSet: false,
    remoteUrl: PRODUCT.origin
  }
}

function installIpc() {
  ipcMain.handle('hermes:boot-progress:get', () => bootProgress)
  ipcMain.handle('hermes:connection', () => ensureConnection())
  ipcMain.handle('hermes:connection:revalidate', async () => {
    connectionPromise = null
    await ensureConnection()
    return { ok: true }
  })
  ipcMain.handle('hermes:gateway:ws-url', () => mintWsTicket().then(ticket => buildGatewayWsUrlWithTicket(PRODUCT.origin, ticket)))
  ipcMain.handle('hermes:api', (_event, request) => apiRequest(request))
  ipcMain.handle('hermes:connection-config:get', () => connectionConfig())
  ipcMain.handle('hermes:profile:get', () => ({ profile: 'default' }))
  ipcMain.handle('hermes:notify', (_event, payload) => {
    if (!Notification.isSupported()) {
      return false
    }

    new Notification({ body: String(payload?.body || ''), title: String(payload?.title || PRODUCT.productName) }).show()
    return true
  })
  ipcMain.handle('hermes:pickAttachmentBytes', pickAttachmentBytes)
  ipcMain.handle('hermes:managed:export-save', (_event, id, name) => saveManagedExport(id, name))
  ipcMain.handle('hermes:connection-config:oauth-login', async () => {
    const tokens = await runNativeLogin(
      PRODUCT.origin,
      {
        openExternal: url => shell.openExternal(url),
        postJson: async (url, body, opts) =>
          (await jsonRequest(url, { body: JSON.stringify(body), method: 'POST', headers: { 'content-type': 'application/json' } }, opts?.timeoutMs)).body
      },
      { oidc: PRODUCT.oidc }
    )
    storeTokens(tokens)
    connectionPromise = null
    return { baseUrl: PRODUCT.origin, connected: true, ok: true }
  })
  ipcMain.handle('hermes:connection-config:oauth-logout', () => {
    clearTokens()
    connectionPromise = null
    return { connected: false, ok: true }
  })
}

function isBundledRendererUrl(rawUrl: string): boolean {
  try {
    const candidate = new URL(rawUrl)
    const bundled = new URL(pathToFileURL(RENDERER_PATH))
    return (
      candidate.protocol === bundled.protocol &&
      candidate.host === bundled.host &&
      candidate.pathname === bundled.pathname &&
      !candidate.search
    )
  } catch {
    return false
  }
}

function installNavigationLockdown(contents: WebContents) {
  const denyNavigation = (event: Electron.Event, rawUrl: string) => {
    if (!isBundledRendererUrl(rawUrl)) {
      event.preventDefault()
    }
  }

  contents.on('will-navigate', denyNavigation)
  contents.on('will-redirect', denyNavigation)
  contents.setWindowOpenHandler(() => ({ action: 'deny' }))
}

function createWindow() {
  mainWindow = new BrowserWindow({
    height: 900,
    minHeight: 640,
    minWidth: 960,
    show: false,
    title: PRODUCT.productName,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: PRELOAD_PATH,
      sandbox: true
    },
    width: 1280
  })
  installNavigationLockdown(mainWindow.webContents)
  mainWindow.once('ready-to-show', () => mainWindow?.show())
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  void mainWindow.loadURL(pathToFileURL(RENDERER_PATH).toString())
}

const lock = app.requestSingleInstanceLock()
if (!lock) {
  app.quit()
} else {
  app.whenReady().then(() => {
    Menu.setApplicationMenu(null)
    app.setAsDefaultProtocolClient(PROTOCOL)
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
    session.defaultSession.setPermissionCheckHandler(() => false)
    installIpc()
    createWindow()
    void ensureConnection().catch(() => undefined)
  })

  app.on('activate', () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      createWindow()
    } else {
      mainWindow.show()
      mainWindow.focus()
    }
  })

  app.on('window-all-closed', () => app.quit())
}
