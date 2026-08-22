import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { getCommitHistory, getManifest } from '../api/client';
import { CommitHistoryPage, Manifest } from '../types';

export default function RepoDetail() {
  const { repoName } = useParams<{ repoName: string }>();
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [history, setHistory] = useState<CommitHistoryPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!repoName) return;
    Promise.all([getManifest(repoName), getCommitHistory(repoName)])
      .then(([m, h]) => {
        setManifest(m);
        setHistory(h);
      })
      .catch((err) => setError(err.message));
  }, [repoName]);

  if (error) {
    return <div className="error">Error loading repository: {error}</div>;
  }

  if (!manifest || !history) {
    return <div className="empty">Loading repository…</div>;
  }

  const totalSize = manifest.files.reduce((sum, f) => sum + f.size, 0);

  return (
    <>
      <div className="card">
        <h2>{repoName}</h2>
        <p>
          Manifest seq <span className="badge">{manifest.seq}</span>
        </p>
        <p>
          {manifest.files.length} file{manifest.files.length !== 1 ? 's' : ''} ({formatBytes(totalSize)})
        </p>
      </div>

      <div className="card">
        <h2>Files</h2>
        {manifest.files.length === 0 ? (
          <p className="empty">No files in this repository.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Path</th>
                <th>Size</th>
                <th>SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {manifest.files.map((file) => (
                <tr key={file.path}>
                  <td>
                    <code>{file.path}</code>
                  </td>
                  <td>{formatBytes(file.size)}</td>
                  <td>
                    <code>{file.sha256.slice(0, 16)}…</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h2>Recent commits</h2>
        {history.records.length === 0 ? (
          <p className="empty">No commits yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Seq</th>
                <th>Message</th>
                <th>Author</th>
                <th>Changes</th>
              </tr>
            </thead>
            <tbody>
              {history.records.map((record) => (
                <tr key={record.seq}>
                  <td>{record.seq}</td>
                  <td>{record.reason || <span className="empty">(no message)</span>}</td>
                  <td>{record.user || '-'}</td>
                  <td>
                    +{record.added.length} -{record.removed.length} ~{record.updated.length}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / k ** i).toFixed(1))} ${sizes[i]}`;
}
