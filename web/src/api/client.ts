import { CommitHistoryPage, Manifest, Repository } from '../types';

const API_BASE = '';

async function api<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function listRepositories(): Promise<Repository[]> {
  return api<Repository[]>('/repos');
}

export async function getManifest(repoName: string): Promise<Manifest> {
  return api<Manifest>(`/repos/${encodeURIComponent(repoName)}/manifest`);
}

export async function getCommitHistory(repoName: string, limit = 20): Promise<CommitHistoryPage> {
  return api<CommitHistoryPage>(
    `/repos/${encodeURIComponent(repoName)}/commits?limit=${limit}`
  );
}
