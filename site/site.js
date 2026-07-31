const header = document.querySelector("[data-header]");
const menuButton = document.querySelector("[data-menu-button]");
const navigation = document.querySelector("[data-navigation]");
const hero = document.querySelector("[data-hero]");
const proof = document.querySelector("[data-proof]");
const sectionVideo = document.querySelector("[data-section-video]");
const revealItems = document.querySelectorAll("[data-reveal]");
const methodSteps = document.querySelectorAll("[data-step]");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const updateHeader = () => {
  header?.classList.toggle("is-scrolled", window.scrollY > 20);
};

const closeMenu = () => {
  navigation?.classList.remove("is-open");
  menuButton?.classList.remove("is-open");
  menuButton?.setAttribute("aria-expanded", "false");
};

const updateHero = () => {
  if (!hero || reducedMotion.matches) {
    return;
  }

  const bounds = hero.getBoundingClientRect();
  const progress = Math.min(Math.max(-bounds.top / Math.max(bounds.height, 1), 0), 1);
  hero.style.setProperty("--hero-progress", progress.toFixed(3));
};

let scrollFrame = 0;
const onScroll = () => {
  updateHeader();

  if (!scrollFrame) {
    scrollFrame = window.requestAnimationFrame(() => {
      updateHero();
      scrollFrame = 0;
    });
  }
};

menuButton?.addEventListener("click", () => {
  const isOpen = navigation?.classList.toggle("is-open") ?? false;
  menuButton.classList.toggle("is-open", isOpen);
  menuButton.setAttribute("aria-expanded", String(isOpen));
});

navigation?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", closeMenu);
});

document.addEventListener("click", (event) => {
  if (!header?.contains(event.target)) {
    closeMenu();
  }
});

hero?.addEventListener("pointermove", (event) => {
  if (reducedMotion.matches || event.pointerType === "touch") {
    return;
  }

  const bounds = hero.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width - 0.5;
  const y = (event.clientY - bounds.top) / bounds.height - 0.5;
  hero.style.setProperty("--pointer-x", `${(x * 12).toFixed(2)}px`);
  hero.style.setProperty("--pointer-y", `${(y * 8).toFixed(2)}px`);
});

hero?.addEventListener("pointerleave", () => {
  hero.style.setProperty("--pointer-x", "0px");
  hero.style.setProperty("--pointer-y", "0px");
});

if (reducedMotion.matches || !("IntersectionObserver" in window)) {
  revealItems.forEach((item) => item.classList.add("is-visible"));
  methodSteps.forEach((item) => item.classList.add("is-visible"));
  proof?.classList.add("is-visible");
} else {
  const revealObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) {
          return;
        }

        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "0px 0px -12% 0px", threshold: 0.12 },
  );

  revealItems.forEach((item) => revealObserver.observe(item));

  const stepObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) {
          return;
        }

        const index = Array.from(methodSteps).indexOf(entry.target);
        window.setTimeout(() => entry.target.classList.add("is-visible"), index * 90);
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.2 },
  );

  methodSteps.forEach((item) => stepObserver.observe(item));

  if (proof) {
    const proofObserver = new IntersectionObserver(
      (entries) => {
        proof.classList.toggle("is-visible", entries.some((entry) => entry.isIntersecting));
      },
      { threshold: 0.2 },
    );
    proofObserver.observe(proof);
  }
}

let videoObserver;

const configureSectionVideo = () => {
  videoObserver?.disconnect();

  if (!sectionVideo) {
    return;
  }

  if (reducedMotion.matches) {
    sectionVideo.pause();
    sectionVideo.currentTime = 0;
    return;
  }

  if (!("IntersectionObserver" in window)) {
    sectionVideo.play().catch(() => {});
    return;
  }

  videoObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          sectionVideo.play().catch(() => {});
        } else {
          sectionVideo.pause();
        }
      });
    },
    { rootMargin: "80px 0px", threshold: 0.2 },
  );
  videoObserver.observe(sectionVideo);
};

reducedMotion.addEventListener("change", () => {
  if (reducedMotion.matches && hero) {
    hero.style.setProperty("--pointer-x", "0px");
    hero.style.setProperty("--pointer-y", "0px");
    hero.style.setProperty("--hero-progress", "0");
  }
  configureSectionVideo();
});

window.addEventListener("scroll", onScroll, { passive: true });
window.addEventListener("resize", updateHero);
configureSectionVideo();
onScroll();
