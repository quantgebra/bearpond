import { NavLink, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import RepoDetail from './pages/RepoDetail';
import Repos from './pages/Repos';

function App() {
  return (
    <>
      <header>
        <div className="container">
          <h1>
            <NavLink to="/">bearpond</NavLink>
          </h1>
          <nav>
            <NavLink to="/" end>
              Repositories
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="container">
        <Layout>
          <Routes>
            <Route path="/" element={<Repos />} />
            <Route path="/repos/:repoName" element={<RepoDetail />} />
          </Routes>
        </Layout>
      </main>
    </>
  );
}

export default App;
