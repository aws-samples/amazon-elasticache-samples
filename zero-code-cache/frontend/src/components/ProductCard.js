import React, { useState } from 'react';

const categoryImages = {
  Laptops: [
    'photo-1496181133206-80ce9b88a853',
    'photo-1525547719571-a2d4ac8945e2',
    'photo-1517336714731-489689fd1ca8',
    'photo-1588872657578-7efd1f1555ed',
    'photo-1531297484001-80022131f5a1',
    'photo-1498050108023-c5249f4df085',
    'photo-1541807084-5c52b6b3adef',
    'photo-1593642702821-c8da6771f0c6',
    'photo-1611186871348-b1ce696e52c9',
    'photo-1602080858428-57174f9431cf',
  ],
  Monitors: [
    'photo-1527443224154-c4a3942d3acf',
    'photo-1547394765-185e1e68f34e',
    'photo-1593640408182-31c70c8268f5',
    'photo-1527443224154-c4a3942d3acf',
    'photo-1547394765-185e1e68f34e',
  ],
  Accessories: [
    'photo-1587829741301-dc798b83add3',
    'photo-1527864550417-7fd91fc51a46',
    'photo-1615663245857-ac93bb7c39e7',
    'photo-1563297007-0686b7003af7',
    'photo-1587829741301-dc798b83add3',
  ],
  Audio: [
    'photo-1505740420928-5e560c06d30e',
    'photo-1583394838336-acd977736f90',
    'photo-1546435770-a3e426bf472b',
    'photo-1484704849700-f032a568e944',
    'photo-1505740420928-5e560c06d30e',
  ],
  Components: [
    'photo-1591799264318-7e6ef8ddb7ea',
    'photo-1555617981-dac3880eac6e',
    'photo-1518770660439-4636190af475',
    'photo-1562976540-1502c2145186',
    'photo-1591799264318-7e6ef8ddb7ea',
  ],
  Networking: [
    'photo-1544197150-b99a580bb7a8',
    'photo-1558494949-ef010cbdcc31',
    'photo-1544197150-b99a580bb7a8',
    'photo-1558494949-ef010cbdcc31',
    'photo-1544197150-b99a580bb7a8',
  ],
  Furniture: [
    'photo-1518455027359-f3f8164ba6bd',
    'photo-1555041469-a586c61ea9bc',
    'photo-1524758631624-e2822e304c36',
    'photo-1518455027359-f3f8164ba6bd',
    'photo-1555041469-a586c61ea9bc',
  ],
  Storage: [
    'photo-1531297484001-80022131f5a1',
    'photo-1518770660439-4636190af475',
    'photo-1555617981-dac3880eac6e',
    'photo-1531297484001-80022131f5a1',
    'photo-1518770660439-4636190af475',
  ],
};

const categoryColors = {
  Laptops: 'bg-indigo-100', Monitors: 'bg-cyan-100', Accessories: 'bg-violet-100',
  Audio: 'bg-rose-100', Components: 'bg-amber-100', Networking: 'bg-teal-100',
  Furniture: 'bg-emerald-100', Storage: 'bg-blue-100',
};

function ProductCard({ product }) {
  const [imgFailed, setImgFailed] = useState(false);
  const images = categoryImages[product.category] || categoryImages.Laptops;
  const imageId = images[product.id % images.length];
  const imageUrl = `https://images.unsplash.com/${imageId}?w=400&h=300&fit=crop&auto=format`;
  const fallbackBg = categoryColors[product.category] || 'bg-gray-100';

  return (
    <div className="bg-white rounded-xl border border-gray-200 hover:border-indigo-300 hover:shadow-md transition-all overflow-hidden group">
      <div className={`h-40 overflow-hidden ${fallbackBg}`}>
        {!imgFailed ? (
          <img src={imageUrl} alt={product.name}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
            onError={() => setImgFailed(true)} />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <span className="text-gray-400 text-sm font-medium">{product.category}</span>
          </div>
        )}
      </div>
      <div className="p-4">
        <h3 className="font-semibold text-gray-900 text-sm mb-0.5 line-clamp-1">{product.name}</h3>
        <p className="text-xs text-gray-400 mb-2">{product.brand} · {product.category}</p>
        <div className="flex items-center gap-1 mb-3">
          <span className="text-sm text-amber-500">{'★'.repeat(Math.round(product.rating))}{'☆'.repeat(5 - Math.round(product.rating))}</span>
          <span className="text-xs text-gray-400">({product.reviewCount.toLocaleString()})</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-lg font-bold text-gray-900">${product.price.toFixed(2)}</span>
          <button className="px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-lg hover:bg-indigo-700 transition-colors">Add to Cart</button>
        </div>
        {!product.inStock && <span className="text-xs text-red-500 mt-1.5 block font-medium">Out of stock</span>}
      </div>
    </div>
  );
}

export default ProductCard;
