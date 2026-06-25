package com.example;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.RequestMapping;

/**
 * Forward non-API, non-asset routes to index.html for React SPA routing.
 * Excludes /api/**, /assets/**, and any path with a file extension.
 */
@Controller
public class SpaController {

    // Only match paths with no dot (no file extension) and not starting with /api or /assets
    @RequestMapping(value = {
        "/",
        "/{path:^(?!api|assets)[^\\.]*$}",
        "/{path:^(?!api|assets)[^\\.]*$}/**"
    })
    public String forward() {
        return "forward:/index.html";
    }
}
