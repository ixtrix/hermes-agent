import { useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { HermesGateway } from './hermes'

type ManagedMessage = {
  id: string
  pending?: boolean
  role: 'assistant' | 'user'
  text: string
}

type ManagedProduct = {
  plane: 'external' | 'internal'
  productId: 'uk.co.scopefurnishing.hermes.external' | 'uk.co.scopefurnishing.hermes.internal'
  productName: 'Scope Hermes External' | 'Scope Hermes Internal'
  remoteOauthConnected: boolean
  remoteUrl: string
}

type ManagedAttachmentDescriptor = {
  attachment_id: string
  metadata: Record<string, number | string>
  mime_type: string
  name: string
  size: number
}

const MANAGED_PRODUCTS = {
  external: {
    productId: 'uk.co.scopefurnishing.hermes.external',
    productName: 'Scope Hermes External'
  },
  internal: {
    productId: 'uk.co.scopefurnishing.hermes.internal',
    productName: 'Scope Hermes Internal'
  }
} as const

const EXPORT_ID_RE = /^exp_[A-Za-z0-9_-]{43}$/

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

function textFrom(value: unknown): string {
  const payload = record(value)
  return typeof payload?.text === 'string' ? payload.text : ''
}

function sessionIdFrom(value: unknown): string | null {
  const payload = record(value)
  const id = payload?.session_id
  return typeof id === 'string' && id ? id : null
}

function managedProductFrom(value: unknown): ManagedProduct {
  const config = record(value)
  if (!config) {
    throw new Error('Managed Hermes product policy is unavailable.')
  }

  const plane = config.plane
  if (plane !== 'internal' && plane !== 'external') {
    throw new Error('Managed Hermes product policy is unavailable.')
  }

  const product = MANAGED_PRODUCTS[plane]
  if (
    config.mode !== 'remote' ||
    config.remoteAuthMode !== 'oauth' ||
    config.productId !== product.productId ||
    typeof config.remoteUrl !== 'string' ||
    !config.remoteUrl
  ) {
    throw new Error('Managed Hermes product policy is invalid.')
  }

  return {
    plane,
    productId: product.productId,
    productName: product.productName,
    remoteOauthConnected: config.remoteOauthConnected === true,
    remoteUrl: config.remoteUrl
  }
}

function dataUrlFromBytes(bytes: Uint8Array, mime: string): string {
  let binary = ''

  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
  }

  return `data:${mime};base64,${btoa(binary)}`
}

function attachmentDescriptorFrom(
  value: unknown,
  picked: { name: string; size: number }
): ManagedAttachmentDescriptor {
  const receipt = record(value)
  const metadata = record(receipt?.metadata)
  const attachmentId = receipt?.attachment_id
  const name = receipt?.name
  const mimeType = receipt?.mime_type
  const size = receipt?.size

  if (
    receipt?.attached !== true ||
    typeof attachmentId !== 'string' ||
    !attachmentId ||
    typeof name !== 'string' ||
    !name ||
    /[\\/]/.test(name) ||
    typeof mimeType !== 'string' ||
    !mimeType.includes('/') ||
    !Number.isSafeInteger(size) ||
    size !== picked.size ||
    !metadata ||
    metadata.purpose !== 'document' ||
    ['identity', 'namespace', 'path', 'file_path', 'ref_path'].some(key => key in metadata) ||
    Object.values(metadata).some(item => typeof item !== 'string' && typeof item !== 'number')
  ) {
    throw new Error(`Managed Hermes did not admit ${picked.name}.`)
  }

  return {
    attachment_id: attachmentId,
    metadata: metadata as Record<string, number | string>,
    mime_type: mimeType,
    name,
    size: size as number
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export default function ManagedApp() {
  const [gateway] = useState(() => new HermesGateway())
  const [product, setProduct] = useState<ManagedProduct | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ManagedMessage[]>([])
  const [attachments, setAttachments] = useState<ManagedAttachmentDescriptor[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [signingIn, setSigningIn] = useState(false)
  const [attaching, setAttaching] = useState(false)
  const [exportId, setExportId] = useState('')
  const [exportName, setExportName] = useState('')
  const [savingExport, setSavingExport] = useState(false)
  const [status, setStatus] = useState('Checking managed sign-in…')

  const createSession = useCallback(
    async (managedProduct: ManagedProduct) => {
      gateway.close()
      const connection = await window.hermesDesktop.getConnection()
      const details = record(connection)

      if (
        details?.mode !== 'remote' ||
        details.plane !== managedProduct.plane ||
        details.productId !== managedProduct.productId
      ) {
        throw new Error('Managed Hermes refused a non-managed connection.')
      }

      await gateway.connect(connection.wsUrl)
      const created = await gateway.request<unknown>('session.create', {
        cols: 96,
        fast: false,
        source: 'desktop',
        ...(connection.profile ? { profile: connection.profile } : {})
      })
      const id = sessionIdFrom(created)

      if (!id) {
        throw new Error('Managed gateway did not create a session.')
      }

      return id
    },
    [gateway]
  )

  useEffect(() => {
    let cancelled = false
    const offState = gateway.onState(state => {
      if (!cancelled) {
        setStatus(state === 'open' ? 'Managed Hermes' : `Managed Hermes: ${state}`)
      }
    })
    const offEvent = gateway.onEvent(event => {
      if (cancelled) {
        return
      }

      if (event.type === 'message.delta') {
        const delta = textFrom(event.payload)
        if (!delta) {
          return
        }
        setMessages(current => {
          const last = current.at(-1)
          if (last?.role === 'assistant' && last.pending) {
            return [...current.slice(0, -1), { ...last, text: last.text + delta }]
          }
          return [...current, { id: `assistant-${Date.now()}`, pending: true, role: 'assistant', text: delta }]
        })
      } else if (event.type === 'message.complete') {
        const text = textFrom(event.payload)
        setMessages(current => {
          const last = current.at(-1)
          if (last?.role === 'assistant' && last.pending) {
            return [...current.slice(0, -1), { ...last, pending: false, ...(text ? { text } : {}) }]
          }
          return text ? [...current, { id: `assistant-${Date.now()}`, role: 'assistant', text }] : current
        })
        setBusy(false)
      } else if (event.type === 'error') {
        const message = textFrom(event.payload) || 'The managed gateway returned an error.'
        setMessages(current => [...current, { id: `error-${Date.now()}`, role: 'assistant', text: message }])
        setBusy(false)
      }
    })

    void (async () => {
      try {
        const managedProduct = managedProductFrom(await window.hermesDesktop.getConnectionConfig())
        if (cancelled) {
          return
        }
        setProduct(managedProduct)

        if (!managedProduct.remoteOauthConnected) {
          setStatus('Sign in to continue.')
          return
        }

        const id = await createSession(managedProduct)
        if (!cancelled) {
          setSessionId(id)
        }
      } catch (error) {
        if (!cancelled) {
          setStatus(errorText(error))
        }
      }
    })()

    return () => {
      cancelled = true
      offState()
      offEvent()
      gateway.close()
    }
  }, [createSession, gateway])

  const signIn = useCallback(async () => {
    if (!product || signingIn) {
      return
    }

    setSigningIn(true)
    setStatus('Opening secure sign-in…')
    try {
      const result = await window.hermesDesktop.oauthLoginConnectionConfig(product.remoteUrl)
      if (!result.ok || !result.connected || result.baseUrl !== product.remoteUrl) {
        throw new Error('Managed Hermes sign-in did not complete.')
      }

      const id = await createSession(product)
      setSessionId(id)
      setProduct({ ...product, remoteOauthConnected: true })
      setStatus('Managed Hermes')
    } catch (error) {
      setStatus(errorText(error))
    } finally {
      setSigningIn(false)
    }
  }, [createSession, product, signingIn])

  const pickAttachments = useCallback(async () => {
    const picker = window.hermesDesktop.pickAttachmentBytes
    if (!picker || !sessionId || attaching || busy) {
      return
    }

    setAttaching(true)
    try {
      const pickedFiles = await picker({
        multiple: true,
        purpose: 'document',
        title: 'Attach files to Hermes'
      })
      const admitted: ManagedAttachmentDescriptor[] = []

      for (const picked of pickedFiles) {
        if (
          !(picked.bytes instanceof Uint8Array) ||
          !picked.name ||
          /[\\/]/.test(picked.name) ||
          picked.purpose !== 'document' ||
          picked.size !== picked.bytes.byteLength ||
          !picked.mime.includes('/')
        ) {
          throw new Error('The managed attachment picker returned an invalid file.')
        }

        const receipt = await gateway.request<unknown>('file.attach', {
          data_url: dataUrlFromBytes(picked.bytes, picked.mime),
          mime: picked.mime,
          mime_type: picked.mime,
          name: picked.name,
          purpose: 'document',
          session_id: sessionId,
          size: picked.size
        })
        admitted.push(attachmentDescriptorFrom(receipt, picked))
      }

      if (admitted.length) {
        setAttachments(current => [...current, ...admitted])
        setStatus(`${admitted.length} attachment${admitted.length === 1 ? '' : 's'} ready`)
      }
    } catch (error) {
      setStatus(errorText(error))
    } finally {
      setAttaching(false)
    }
  }, [attaching, busy, gateway, sessionId])

  const submit = useCallback(async () => {
    const text = draft.trim()
    if ((!text && attachments.length === 0) || !sessionId || busy || attaching) {
      return
    }

    setDraft('')
    setBusy(true)
    const displayText = text || `Attached ${attachments.map(attachment => attachment.name).join(', ')}`
    setMessages(current => [...current, { id: `user-${Date.now()}`, role: 'user', text: displayText }])
    try {
      await gateway.request('prompt.submit', {
        attachments,
        session_id: sessionId,
        text
      })
      setAttachments([])
    } catch (error) {
      setBusy(false)
      setMessages(current => [
        ...current,
        { id: `error-${Date.now()}`, role: 'assistant', text: errorText(error) }
      ])
    }
  }, [attachments, attaching, busy, draft, gateway, sessionId])

  const saveExport = useCallback(async () => {
    const id = exportId.trim()
    const saver = window.hermesDesktop.saveExport
    if (!saver || !EXPORT_ID_RE.test(id) || savingExport) {
      return
    }

    setSavingExport(true)
    try {
      const result = await saver(id, exportName.trim() || undefined)
      setStatus(result.ok ? 'Export saved.' : 'Export save canceled.')
      if (result.ok) {
        setExportId('')
        setExportName('')
      }
    } catch (error) {
      setStatus(errorText(error))
    } finally {
      setSavingExport(false)
    }
  }, [exportId, exportName, savingExport])

  if (!sessionId) {
    return (
      <main
        style={{
          alignItems: 'center',
          background: '#111827',
          color: '#f9fafb',
          display: 'flex',
          height: '100vh',
          justifyContent: 'center'
        }}
      >
        <section style={{ maxWidth: 480, padding: 32, textAlign: 'center' }}>
          <h1>{product?.productName ?? 'Scope Hermes'}</h1>
          <p style={{ color: '#9ca3af', lineHeight: 1.5 }}>
            Sign in with your Scope account to start a managed, remote-only Hermes session.
          </p>
          <button disabled={!product || signingIn} onClick={() => void signIn()} type="button">
            {signingIn ? 'Signing in…' : 'Sign in'}
          </button>
          <p aria-live="polite" style={{ color: '#9ca3af', marginTop: 18 }}>
            {status}
          </p>
        </section>
      </main>
    )
  }

  return (
    <main style={{ background: '#111827', color: '#f9fafb', display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <header style={{ borderBottom: '1px solid #374151', padding: '18px 24px' }}>
        <strong>{product?.productName ?? 'Scope Hermes'}</strong>
        <span aria-live="polite" style={{ color: '#9ca3af', marginLeft: 12 }}>
          {status}
        </span>
      </header>
      <section aria-label="Hermes output" aria-live="polite" style={{ flex: 1, overflow: 'auto', padding: 24 }}>
        {messages.length === 0 ? (
          <p style={{ color: '#9ca3af', margin: '0 auto', maxWidth: 860 }}>Hermes output will appear here.</p>
        ) : null}
        {messages.map(message => (
          <article key={message.id} style={{ margin: '0 auto 18px', maxWidth: 860, whiteSpace: 'pre-wrap' }}>
            <strong>{message.role === 'user' ? 'You' : 'Hermes'}</strong>
            <div style={{ marginTop: 6 }}>{message.text}</div>
          </article>
        ))}
      </section>
      <section
        aria-label="Released export"
        style={{ borderTop: '1px solid #374151', display: 'flex', flexWrap: 'wrap', gap: 10, padding: '12px 18px' }}
      >
        <input
          aria-label="Released Export ID"
          onChange={event => setExportId(event.target.value)}
          placeholder="Released Export ID (exp_…)"
          style={{ background: '#1f2937', border: '1px solid #4b5563', borderRadius: 6, color: 'inherit', padding: 8 }}
          value={exportId}
        />
        <input
          aria-label="Suggested export filename"
          onChange={event => setExportName(event.target.value)}
          placeholder="Suggested filename (optional)"
          style={{ background: '#1f2937', border: '1px solid #4b5563', borderRadius: 6, color: 'inherit', padding: 8 }}
          value={exportName}
        />
        <button disabled={!EXPORT_ID_RE.test(exportId.trim()) || savingExport} onClick={() => void saveExport()} type="button">
          {savingExport ? 'Saving…' : 'Save released export'}
        </button>
      </section>
      {attachments.length ? (
        <section aria-label="Attachments" style={{ borderTop: '1px solid #374151', padding: '10px 18px' }}>
          {attachments.map(attachment => (
            <span key={attachment.attachment_id} style={{ display: 'inline-block', marginRight: 12 }}>
              {attachment.name} ({attachment.size.toLocaleString()} bytes){' '}
              <button
                disabled={busy}
                onClick={() =>
                  setAttachments(current => current.filter(item => item.attachment_id !== attachment.attachment_id))
                }
                type="button"
              >
                Remove
              </button>
            </span>
          ))}
        </section>
      ) : null}
      <form
        onSubmit={event => {
          event.preventDefault()
          void submit()
        }}
        style={{ borderTop: '1px solid #374151', display: 'flex', gap: 12, padding: 18 }}
      >
        <button disabled={busy || attaching} onClick={() => void pickAttachments()} type="button">
          {attaching ? 'Attaching…' : 'Attach file'}
        </button>
        <textarea
          aria-label="Message Hermes"
          disabled={busy}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              void submit()
            }
          }}
          placeholder="Message Hermes…"
          rows={3}
          style={{
            background: '#1f2937',
            border: '1px solid #4b5563',
            borderRadius: 8,
            color: 'inherit',
            flex: 1,
            padding: 12,
            resize: 'vertical'
          }}
          value={draft}
        />
        <button disabled={busy || attaching || (!draft.trim() && attachments.length === 0)} type="submit">
          Send
        </button>
      </form>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<ManagedApp />)
