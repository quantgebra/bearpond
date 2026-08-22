import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listRepositories } from '../api/client';
import { Repository } from '../types';

export default function Repos() {
  const [repos, setRepos] = useState<Repository[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listRepositories()
      .then(setRepos)
      .catch((err) => setError(err.message));
  }, []);

  if (error) {
    return <div className="error">Error loading repositories: {error}</div>;
  }

  if (repos === null) {
    return <div className="empty">Loading repositories…</div>;
  }

  if (repos.length === 0) {
    return (
      <div className="card">
        <h2>Repositories</h2>
        <p className="empty">No repositories yet.</p>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Repositories</h2>
      <table>
        <thead>
          <tr>
            <th>Name</th>
          </tr>
        </thead>
        <tbody>
          {repos.map((repo) => (
            <tr key={repo.name}>
              <td>
                <Link to={`/repos/${encodeURIComponent(repo.name)}`}>{repo.name}</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
