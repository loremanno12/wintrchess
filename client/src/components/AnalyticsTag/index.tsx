import React, { useEffect } from "react";

const googleTagURL = "https://www.googletagmanager.com/gtag/js?id=";
const measurementId = process.env.ANALYTICS_MEASUREMENT_ID;

function AnalyticsTag() {
    if (!measurementId) return;

    useEffect(() => {
        window.dataLayer ??= [];

        const gtag = (...args: any[]) => (
            window.dataLayer.push(...args)
        );

        gtag("js", new Date());
        gtag("config", measurementId);
    });

    return <script async src={googleTagURL + measurementId} />;
}

export default AnalyticsTag;