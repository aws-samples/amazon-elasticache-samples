import React, { useState } from 'react';

function SearchBar({ onSearch, placeholder }) {
  const [query, setQuery] = useState('');
  const handleSubmit = (e) => { e.preventDefault(); if (query.trim()) onSearch(query.trim()); };

  return (
    <form onSubmit={handleSubmit} className="flex-1 max-w-xl mx-8">
      <div className="relative">
        <input type="text" value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder={placeholder || "Search..."}
          className="w-full px-4 py-2.5 pl-10 bg-gray-50 border border-gray-200 rounded-xl text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-400"
          aria-label="Search" />
        <span className="absolute left-3.5 top-3 text-gray-400">🔍</span>
        <button type="submit" className="absolute right-1.5 top-1.5 px-4 py-1.5 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 transition-colors">
          Search
        </button>
      </div>
    </form>
  );
}

export default SearchBar;
