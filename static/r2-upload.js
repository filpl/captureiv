/* Browser-to-R2 multipart transfer with server-verified ownership and resumable parts. */
(() => {
  async function api(url, csrf, body = {}) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(body),
      credentials: 'same-origin',
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `Upload request failed (${response.status}).`);
    return result;
  }

  function putPart(url, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', url);
      xhr.upload.onprogress = event => {
        if (event.lengthComputable) onProgress(event.loaded);
      };
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300
        ? resolve() : reject(new Error(`R2 rejected an upload part (${xhr.status}).`));
      xhr.onerror = () => reject(new Error('Upload interrupted. Retry to resume the completed parts.'));
      xhr.ontimeout = () => reject(new Error('Upload timed out. Retry to resume the completed parts.'));
      xhr.send(blob);
    });
  }

  async function uploadFile(file, metadata, { csrf, onProgress = () => {} }) {
    const state = await api('/admin/r2-upload/start', csrf, {
      ...metadata,
      filename: file.name,
      size: file.size,
      lastModified: file.lastModified || 0,
    });
    if (state.status === 'completed' && state.completedFlightId) {
      onProgress(file.size, file.size);
      return { id: state.completedFlightId, duplicate: true };
    }
    if (state.status !== 'uploading' || !state.uploadId || !state.partSize) {
      throw new Error('The upload session is not ready. Retry in a moment.');
    }

    const completed = new Map((state.uploadedParts || []).map(part => [part.partNumber, part.size]));
    let completedBytes = [...completed.values()].reduce((sum, size) => sum + size, 0);
    onProgress(completedBytes, file.size);
    for (let partNumber = 1; partNumber <= state.partCount; partNumber += 1) {
      if (completed.has(partNumber)) continue;
      const start = (partNumber - 1) * state.partSize;
      const end = Math.min(file.size, start + state.partSize);
      const blob = file.slice(start, end);
      const signed = await api(`/admin/r2-upload/${encodeURIComponent(state.uploadId)}/part-url`, csrf,
        { partNumber });
      let partLoaded = 0;
      await putPart(signed.url, blob, loaded => {
        partLoaded = loaded;
        onProgress(completedBytes + partLoaded, file.size);
      });
      completedBytes += blob.size;
      onProgress(completedBytes, file.size);
    }
    return api(`/admin/r2-upload/${encodeURIComponent(state.uploadId)}/complete`, csrf, {});
  }

  window.uploadR2Multipart = uploadFile;
})();
