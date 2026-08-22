import { ReactNode } from 'react';

interface LayoutProps {
  children: ReactNode;
}

export default function Layout({ children }: LayoutProps) {
  return <div style={{ paddingTop: '1rem' }}>{children}</div>;
}
