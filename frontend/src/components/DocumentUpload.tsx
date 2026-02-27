import { useState, useRef, useCallback, useEffect } from 'react';
import {
  Upload,
  FileText,
  X,
  Clock,
  CheckCircle,
  AlertCircle,
  Loader2,
  Trash2,
  FolderArchive,
  Play,
  Square,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import type { PipelineStatus } from '../types';

interface PipelineResult {
  status: string;
  org_name?: string;
  total_uploaded?: number;
  valid_files?: number;
  unique_files?: number;
  documents_persisted?: number;
  elapsed_seconds?: number;
  error?: string;
}

interface DocumentUploadProps {
  onUploadComplete: () => void;
  onClearDatabase: () => Promise<void>;
  selectedOrg: string;
}

export default function DocumentUpload({ onUploadComplete, onClearDatabase, selectedOrg }: DocumentUploadProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<PipelineResult | null>(null);
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Pipeline run/stop state
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    const poll = async () => {
      try {
        const res = await fetch('/api/pipeline/status');
        if (res.ok) {
          const data: PipelineStatus = await res.json();
          setPipelineStatus(data);
          if (data.state !== 'processing') {
            stopPolling();
            if (data.state === 'done') {
              onUploadComplete();
            }
          }
        }
      } catch {
        // ignore network errors during polling
      }
    };
    poll(); // immediate first check
    pollRef.current = setInterval(poll, 1500);
  }, [stopPolling, onUploadComplete]);

  // On mount, check if pipeline is already running and resume polling
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch('/api/pipeline/status');
        if (res.ok) {
          const data: PipelineStatus = await res.json();
          setPipelineStatus(data);
          if (data.state === 'processing') {
            startPolling();
          }
        }
      } catch {
        // API not available
      }
    })();
    return () => stopPolling();
  }, [startPolling, stopPolling]);

  const addFiles = useCallback((incoming: FileList | File[]) => {
    const arr = Array.from(incoming);
    setFiles((prev) => {
      const existing = new Set(prev.map((f) => f.name + f.size));
      const unique = arr.filter((f) => !existing.has(f.name + f.size));
      return [...prev, ...unique];
    });
    setResult(null);
  }, []);

  const removeFile = useCallback((index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer.files.length > 0) {
        addFiles(e.dataTransfer.files);
      }
    },
    [addFiles]
  );

  const startTimer = useCallback(() => {
    setElapsed(0);
    const start = Date.now();
    timerRef.current = setInterval(() => {
      setElapsed(Math.round((Date.now() - start) / 100) / 10);
    }, 100);
  }, []);

  const stopTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleUpload = useCallback(async () => {
    if (files.length === 0) return;
    setUploading(true);
    setResult(null);
    startTimer();

    const formData = new FormData();
    files.forEach((f) => formData.append('files', f));

    try {
      const res = await fetch('/api/upload', { method: 'POST', body: formData });
      const data: PipelineResult = await res.json();
      stopTimer();
      setResult(data);
      if (data.status === 'ok') {
        setFiles([]);
        onUploadComplete();
      }
    } catch (err) {
      stopTimer();
      setResult({ status: 'error', error: String(err) });
    } finally {
      setUploading(false);
    }
  }, [files, onUploadComplete, startTimer, stopTimer]);

  const handleRunPipeline = useCallback(async () => {
    if (!selectedOrg) return;
    setPipelineStatus(null);
    try {
      const res = await fetch(`/api/pipeline/run?org_name=${encodeURIComponent(selectedOrg)}`, {
        method: 'POST',
      });
      const data = await res.json();
      if (data.status === 'ok') {
        startPolling();
      } else {
        setPipelineStatus({
          state: 'error',
          org_name: selectedOrg,
          current_stage: null,
          current_stage_label: null,
          total_docs: 0,
          processed_docs: 0,
          stages_completed: [],
          elapsed_seconds: null,
          error: data.error || 'Failed to start pipeline',
        });
      }
    } catch (err) {
      setPipelineStatus({
        state: 'error',
        org_name: selectedOrg,
        current_stage: null,
        current_stage_label: null,
        total_docs: 0,
        processed_docs: 0,
        stages_completed: [],
        elapsed_seconds: null,
        error: String(err),
      });
    }
  }, [selectedOrg, startPolling]);

  const handleStopPipeline = useCallback(async () => {
    try {
      await fetch('/api/pipeline/stop', { method: 'POST' });
    } catch {
      // ignore
    }
  }, []);

  const handleClear = useCallback(async () => {
    setClearing(true);
    try {
      await onClearDatabase();
      setConfirmClear(false);
      setResult(null);
      setFiles([]);
      setPipelineStatus(null);
    } finally {
      setClearing(false);
    }
  }, [onClearDatabase]);

  const formatBytes = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const hasZip = files.some((f) => f.name.toLowerCase().endsWith('.zip'));

  const isPipelineProcessing = pipelineStatus?.state === 'processing';
  const isPipelineTerminal =
    pipelineStatus?.state === 'done' ||
    pipelineStatus?.state === 'error' ||
    pipelineStatus?.state === 'cancelled';

  const pipelineDocProgress =
    pipelineStatus && pipelineStatus.total_docs > 0
      ? Math.round((pipelineStatus.processed_docs / pipelineStatus.total_docs) * 100)
      : 0;

  // Stages 1-3 have per-doc progress
  const showDocProgress =
    isPipelineProcessing &&
    pipelineStatus?.current_stage &&
    ['stage_1', 'stage_2', 'stage_3'].includes(pipelineStatus.current_stage);

  return (
    <div className="bg-white rounded-lg shadow-sm border border-slate-200 overflow-hidden">
      {/* Card header */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200 bg-slate-50/50">
        <div className="flex items-center gap-2">
          <Upload className="w-4 h-4 text-slate-500" />
          <h2 className="text-sm font-semibold text-slate-700">Document Ingestion</h2>
        </div>

        <div className="flex items-center gap-2">
          {/* Run Pipeline button */}
          {!isPipelineProcessing && (
            <button
              type="button"
              onClick={handleRunPipeline}
              disabled={!selectedOrg || uploading}
              className={clsx(
                'flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md transition-colors cursor-pointer',
                !selectedOrg || uploading
                  ? 'text-slate-400 bg-slate-100 cursor-not-allowed'
                  : 'text-emerald-700 bg-emerald-50 hover:bg-emerald-100 border border-emerald-200'
              )}
            >
              <Play className="w-3.5 h-3.5" />
              Run Pipeline
            </button>
          )}

          {/* Stop Pipeline button */}
          {isPipelineProcessing && (
            <button
              type="button"
              onClick={handleStopPipeline}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-red-700 bg-red-50 hover:bg-red-100 border border-red-200 rounded-md transition-colors cursor-pointer"
            >
              <Square className="w-3.5 h-3.5" />
              Stop Pipeline
            </button>
          )}

          {/* Clear database button */}
          {!confirmClear ? (
            <button
              type="button"
              onClick={() => setConfirmClear(true)}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-slate-500 hover:text-red-600 hover:bg-red-50 rounded-md transition-colors cursor-pointer"
            >
              <Trash2 className="w-3.5 h-3.5" />
              Clear Database
            </button>
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-xs text-red-600 font-medium">Delete all data?</span>
              <button
                type="button"
                onClick={handleClear}
                disabled={clearing}
                className="px-2.5 py-1 text-xs font-medium text-white bg-red-600 rounded-md hover:bg-red-700 disabled:opacity-50 transition-colors cursor-pointer"
              >
                {clearing ? 'Clearing...' : 'Confirm'}
              </button>
              <button
                type="button"
                onClick={() => setConfirmClear(false)}
                className="px-2.5 py-1 text-xs font-medium text-slate-500 bg-slate-100 rounded-md hover:bg-slate-200 transition-colors cursor-pointer"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Drop zone */}
      <div
        className={clsx(
          'mx-5 mt-4 mb-3 border-2 border-dashed rounded-lg p-5 text-center transition-all cursor-pointer',
          dragOver
            ? 'border-blue-400 bg-blue-50/50'
            : 'border-slate-200 hover:border-slate-300 hover:bg-slate-50/50',
          uploading && 'pointer-events-none opacity-50'
        )}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.doc,.html,.htm,.xlsx,.xls,.png,.jpg,.jpeg,.zip,.msg"
          className="hidden"
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files);
            e.target.value = '';
          }}
        />
        <div className="flex items-center justify-center gap-3">
          <div className={clsx(
            'p-2.5 rounded-lg',
            dragOver ? 'bg-blue-100' : 'bg-slate-100'
          )}>
            <FolderArchive
              className={clsx('w-6 h-6', dragOver ? 'text-blue-500' : 'text-slate-400')}
            />
          </div>
          <div className="text-left">
            <p className="text-sm font-medium text-slate-700">
              Drop files or a <span className="text-blue-600">.zip</span> organization folder
            </p>
            <p className="text-xs text-slate-400 mt-0.5">
              PDF, DOCX, HTML, XLSX, PNG, JPG, ZIP, MSG
            </p>
          </div>
        </div>
      </div>

      {/* File list */}
      {files.length > 0 && (
        <div className="mx-5 mb-3">
          <div className="border border-slate-200 rounded-lg overflow-hidden">
            <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
              <span className="text-xs font-medium text-slate-600">
                {files.length} file{files.length > 1 ? 's' : ''} selected
                <span className="text-slate-400 ml-1.5">
                  ({formatBytes(files.reduce((sum, f) => sum + f.size, 0))})
                </span>
                {hasZip && (
                  <span className="ml-2 inline-flex items-center gap-1 text-blue-600">
                    <FolderArchive className="w-3 h-3" />
                    zip
                  </span>
                )}
              </span>
              <button
                type="button"
                onClick={handleUpload}
                disabled={uploading}
                className={clsx(
                  'flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded-md transition-colors',
                  uploading
                    ? 'bg-blue-400 text-white cursor-not-allowed'
                    : 'bg-blue-600 text-white hover:bg-blue-700'
                )}
              >
                {uploading ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Processing
                  </>
                ) : (
                  <>
                    <Upload className="w-3.5 h-3.5" />
                    Upload & Process
                  </>
                )}
              </button>
            </div>

            <ul className="divide-y divide-slate-100 max-h-40 overflow-y-auto">
              {files.map((file, i) => (
                <li key={file.name + file.size} className="flex items-center gap-3 px-4 py-2">
                  <FileText className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="text-xs text-slate-700 truncate flex-1">{file.name}</span>
                  <span className="text-xs text-slate-400 tabular-nums shrink-0">
                    {formatBytes(file.size)}
                  </span>
                  {!uploading && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        removeFile(i);
                      }}
                      className="text-slate-300 hover:text-red-500 transition-colors"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* Upload processing timer */}
      {uploading && (
        <div className="mx-5 mb-3 flex items-center gap-3 px-4 py-2.5 bg-blue-50 border border-blue-200 rounded-lg">
          <Loader2 className="w-4 h-4 text-blue-600 animate-spin shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-xs font-medium text-blue-800">Processing documents...</p>
            <p className="text-xs text-blue-500 truncate">
              Validating, deduplicating, and persisting
            </p>
          </div>
          <div className="flex items-center gap-1 text-xs font-mono font-semibold text-blue-700 bg-blue-100 px-2 py-0.5 rounded shrink-0">
            <Clock className="w-3 h-3" />
            {elapsed.toFixed(1)}s
          </div>
        </div>
      )}

      {/* Pipeline progress panel */}
      {isPipelineProcessing && pipelineStatus && (
        <div className="mx-5 mb-3 px-4 py-3 bg-indigo-50 border border-indigo-200 rounded-lg space-y-2.5">
          {/* Current stage + spinner */}
          <div className="flex items-center gap-3">
            <Loader2 className="w-4 h-4 text-indigo-600 animate-spin shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium text-indigo-800">
                {pipelineStatus.current_stage_label || 'Processing...'}
              </p>
            </div>
            {pipelineStatus.elapsed_seconds != null && (
              <div className="flex items-center gap-1 text-xs font-mono font-semibold text-indigo-700 bg-indigo-100 px-2 py-0.5 rounded shrink-0">
                <Clock className="w-3 h-3" />
                {pipelineStatus.elapsed_seconds.toFixed(1)}s
              </div>
            )}
          </div>

          {/* Per-doc progress bar for stages 1-3 */}
          {showDocProgress && (
            <div className="space-y-1">
              <div className="flex items-center justify-between text-xs text-indigo-600">
                <span>
                  Documents: {pipelineStatus.processed_docs}/{pipelineStatus.total_docs}
                </span>
                <span className="font-mono">{pipelineDocProgress}%</span>
              </div>
              <div className="w-full bg-indigo-100 rounded-full h-1.5">
                <div
                  className="bg-indigo-500 h-1.5 rounded-full transition-all duration-300"
                  style={{ width: `${pipelineDocProgress}%` }}
                />
              </div>
            </div>
          )}

          {/* Completed stages as chips */}
          {pipelineStatus.stages_completed.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {pipelineStatus.stages_completed.map((stage) => (
                <span
                  key={stage}
                  className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium text-emerald-700 bg-emerald-100 rounded-full"
                >
                  <CheckCircle className="w-3 h-3" />
                  {stage}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Pipeline result banners */}
      {isPipelineTerminal && pipelineStatus && (
        <div
          className={clsx(
            'mx-5 mb-3 flex items-center gap-3 px-4 py-2.5 rounded-lg border',
            pipelineStatus.state === 'done' && 'bg-emerald-50 border-emerald-200',
            pipelineStatus.state === 'error' && 'bg-red-50 border-red-200',
            pipelineStatus.state === 'cancelled' && 'bg-amber-50 border-amber-200'
          )}
        >
          {pipelineStatus.state === 'done' && (
            <CheckCircle className="w-4 h-4 text-emerald-600 shrink-0" />
          )}
          {pipelineStatus.state === 'error' && (
            <AlertCircle className="w-4 h-4 text-red-600 shrink-0" />
          )}
          {pipelineStatus.state === 'cancelled' && (
            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0" />
          )}
          <div className="flex-1 min-w-0">
            {pipelineStatus.state === 'done' && (
              <p className="text-xs text-emerald-800">
                <span className="font-semibold">Pipeline complete</span>
                {' — '}
                {pipelineStatus.stages_completed.length} stages finished
                {pipelineStatus.org_name && (
                  <span className="text-emerald-600 ml-1">
                    for {pipelineStatus.org_name}
                  </span>
                )}
              </p>
            )}
            {pipelineStatus.state === 'error' && (
              <p className="text-xs text-red-700">
                <span className="font-semibold">Pipeline failed</span>
                {pipelineStatus.error && ` — ${pipelineStatus.error}`}
              </p>
            )}
            {pipelineStatus.state === 'cancelled' && (
              <p className="text-xs text-amber-700">
                <span className="font-semibold">Pipeline cancelled</span>
                {pipelineStatus.stages_completed.length > 0 && (
                  <span className="text-amber-600 ml-1">
                    after {pipelineStatus.stages_completed[pipelineStatus.stages_completed.length - 1]}
                  </span>
                )}
              </p>
            )}
          </div>
          {pipelineStatus.elapsed_seconds != null && (
            <span className="flex items-center gap-1 text-xs font-mono text-slate-400 shrink-0">
              <Clock className="w-3 h-3" />
              {pipelineStatus.elapsed_seconds.toFixed(1)}s
            </span>
          )}
        </div>
      )}

      {/* Upload result */}
      {result && !uploading && (
        <div
          className={clsx(
            'mx-5 mb-3 flex items-center gap-3 px-4 py-2.5 rounded-lg border',
            result.status === 'ok'
              ? 'bg-emerald-50 border-emerald-200'
              : 'bg-red-50 border-red-200'
          )}
        >
          {result.status === 'ok' ? (
            <CheckCircle className="w-4 h-4 text-emerald-600 shrink-0" />
          ) : (
            <AlertCircle className="w-4 h-4 text-red-600 shrink-0" />
          )}
          <div className="flex-1 min-w-0">
            {result.status === 'ok' ? (
              <p className="text-xs text-emerald-800">
                <span className="font-semibold">{result.org_name}</span>
                {' — '}
                {result.documents_persisted} doc{result.documents_persisted !== 1 ? 's' : ''} persisted
                <span className="text-emerald-600 ml-1">
                  ({result.total_uploaded} uploaded, {result.valid_files} valid, {result.unique_files} unique)
                </span>
              </p>
            ) : (
              <p className="text-xs text-red-700">{result.error}</p>
            )}
          </div>
          <span className="flex items-center gap-1 text-xs font-mono text-slate-400 shrink-0">
            <Clock className="w-3 h-3" />
            {result.elapsed_seconds?.toFixed(1)}s
          </span>
        </div>
      )}

      {/* Bottom padding when nothing below the drop zone */}
      {files.length === 0 && !uploading && !result && !isPipelineProcessing && !isPipelineTerminal && <div className="h-2" />}
    </div>
  );
}
