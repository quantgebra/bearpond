// runs before the page is drawn, so that a page in the light theme never flashes dark first
document.documentElement.dataset.theme = localStorage.getItem("bearpond-theme") || "dark";
